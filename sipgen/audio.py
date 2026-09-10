"""音声データの読み込み・リサンプル・G.711 エンコード。

RTP に載せるまでの音声はすべて「8kHz / モノラル / 16bit PCM の int リスト」
という 1 つの内部表現に揃える。外から来る WAV の形式差はここで吸収する。
標準ライブラリだけで完結させたいので audioop には依存しない
(audioop は Python 3.13 で削除されたため)。
"""

import struct
import wave

SAMPLE_RATE = 8000  # 電話帯域。RTP のクロックレートもこれに揃える


# --- G.711 変換テーブル -------------------------------------------------
# ITU-T G.711 の実装 (Sun の g711.c と同じセグメント境界) を素直に移植し、
# 全 16bit 値ぶんのテーブルを起動時に一度だけ作る。1 サンプルずつ関数を
# 呼ぶとフレーム数ぶん Python のループが回って遅いため。

_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _search(val, table):
    for i, limit in enumerate(table):
        if val <= limit:
            return i
    return len(table)


def _linear_to_ulaw(pcm):
    pcm >>= 2
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF
    if pcm > 8159:
        pcm = 8159
    pcm += 33  # BIAS(0x84) >> 2
    seg = _search(pcm, _SEG_UEND)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm >> (seg + 1)) & 0x0F)) ^ mask


def _linear_to_alaw(pcm):
    pcm >>= 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = _search(pcm, _SEG_AEND)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (pcm >> 1) & 0x0F
    else:
        aval |= (pcm >> seg) & 0x0F
    return aval ^ mask


def _build_encode_table(fn):
    # index = サンプル値 + 32768 で引けるようにしておく
    return bytes(fn(v) for v in range(-32768, 32768))


_ULAW_ENCODE = _build_encode_table(_linear_to_ulaw)
_ALAW_ENCODE = _build_encode_table(_linear_to_alaw)


def _build_ulaw_decode():
    table = []
    for u in range(256):
        u = ~u & 0xFF
        t = ((u & 0x0F) << 3) + 0x84
        t <<= (u & 0x70) >> 4
        # 反転後の符号ビットが立っていれば負side。G.711 の μ-law は
        # 符号ビットの意味が A-law と逆なので注意
        table.append(0x84 - t if u & 0x80 else t - 0x84)
    return table


def _build_alaw_decode():
    table = []
    for a in range(256):
        a ^= 0x55
        t = (a & 0x0F) << 4
        seg = (a & 0x70) >> 4
        if seg == 0:
            t += 8
        elif seg == 1:
            t += 0x108
        else:
            t = (t + 0x108) << (seg - 1)
        # A-law は符号ビットが立っている側が正
        table.append(t if a & 0x80 else -t)
    return table


_ULAW_DECODE = _build_ulaw_decode()
_ALAW_DECODE = _build_alaw_decode()

# SDP / RTP のペイロードタイプ。RFC 3551 の静的割り当て。
CODECS = {
    "PCMU": {"pt": 0, "name": "PCMU", "encode": _ULAW_ENCODE, "silence": 0xFF},
    "PCMA": {"pt": 8, "name": "PCMA", "encode": _ALAW_ENCODE, "silence": 0xD5},
}


def encode(samples, codec):
    """16bit PCM の列を G.711 のバイト列にする。"""
    table = CODECS[codec]["encode"]
    return bytes(table[s + 32768] for s in samples)


def silence(n_samples):
    return [0] * n_samples


def seconds_to_samples(seconds):
    return int(round(seconds * SAMPLE_RATE))


# --- WAV 読み込み -------------------------------------------------------

def read_wav(path):
    """WAV を 8kHz モノラル 16bit PCM に正規化して返す。

    wave モジュールは G.711 の WAV (fmt タグ 6/7) を開けないので、
    その場合だけ自前で RIFF を読む。
    """
    try:
        with wave.open(path, "rb") as wf:
            width = wf.getsampwidth()
            channels = wf.getnchannels()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        samples = _decode_pcm(raw, width)
    except wave.Error:
        samples, channels, rate = _read_g711_wav(path)

    if channels > 1:
        samples = _downmix(samples, channels)
    if rate != SAMPLE_RATE:
        samples = _resample(samples, rate, SAMPLE_RATE)
    return samples


def _decode_pcm(raw, width):
    if width == 2:
        return list(struct.unpack("<%dh" % (len(raw) // 2), raw[: len(raw) // 2 * 2]))
    if width == 1:
        # 8bit WAV は符号なし (0..255)
        return [(b - 128) << 8 for b in raw]
    if width == 4:
        vals = struct.unpack("<%di" % (len(raw) // 4), raw[: len(raw) // 4 * 4])
        return [v >> 16 for v in vals]
    raise ValueError("対応していないサンプル幅です: %d バイト" % width)


def _read_g711_wav(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("WAV ファイルとして読めません: %s" % path)
    pos = 12
    fmt_tag = channels = rate = None
    payload = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt_tag, channels, rate = struct.unpack("<HHI", body[:8])
        elif cid == b"data":
            payload = body
        pos += 8 + size + (size & 1)
    if fmt_tag is None or payload is None:
        raise ValueError("fmt/data チャンクが見つかりません: %s" % path)
    if fmt_tag == 7:
        samples = [_ULAW_DECODE[b] for b in payload]
    elif fmt_tag == 6:
        samples = [_ALAW_DECODE[b] for b in payload]
    else:
        raise ValueError("対応していない WAV 形式です (fmt タグ %d): %s" % (fmt_tag, path))
    return samples, channels, rate


def _downmix(samples, channels):
    out = []
    for i in range(0, len(samples) - channels + 1, channels):
        out.append(sum(samples[i:i + channels]) // channels)
    return out


def _resample(samples, src_rate, dst_rate):
    """線形補間のリサンプラ。

    通話音声のテスト素材としてはこれで十分な品質が出るので、
    外部ライブラリを増やしてまで高次の補間はしない。
    """
    if not samples:
        return []
    ratio = src_rate / dst_rate
    n_out = int(len(samples) / ratio)
    out = []
    last = len(samples) - 1
    for i in range(n_out):
        pos = i * ratio
        idx = int(pos)
        frac = pos - idx
        a = samples[idx]
        b = samples[idx + 1] if idx < last else a
        out.append(int(a + (b - a) * frac))
    return out


def write_wav(path, samples, rate=SAMPLE_RATE):
    """確認用の 16bit モノラル WAV を書き出す。"""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        clipped = [max(-32768, min(32767, s)) for s in samples]
        wf.writeframes(struct.pack("<%dh" % len(clipped), *clipped))


def normalize(samples, peak=26000):
    """ピーク値を揃える。TTS 音声と持ち込み WAV の音量差を消すため。"""
    if not samples:
        return samples
    current = max(abs(s) for s in samples)
    if current == 0:
        return samples
    gain = peak / current
    return [max(-32768, min(32767, int(s * gain))) for s in samples]
