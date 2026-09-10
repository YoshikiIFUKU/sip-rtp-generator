"""Windows 標準の音声合成 (System.Speech / SAPI5) で発話 WAV を作る。

外部サービスや追加ライブラリを使わずに済ませたいので、PowerShell 経由で
OS の合成エンジンを呼ぶ。同じ文章を何度も合成すると遅いため、
入力のハッシュをキーにして WAV をキャッシュする。
"""

import hashlib
import os
import sys
import xml.sax.saxutils as sax

from . import proc

_HERE = os.path.dirname(os.path.abspath(__file__))


class TtsError(Exception):
    pass


def script_path():
    """speak.ps1 の場所。

    PyInstaller で 1 ファイルにまとめると、実行時にはテンポラリへ展開された
    sys._MEIPASS の下に置かれる。配布形態を問わず見つかるようにする。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return os.path.join(bundled, "sipgen", "speak.ps1")
    return os.path.join(_HERE, "speak.ps1")


def _powershell(args):
    try:
        return proc.powershell(args)
    except proc.ProcessError as exc:
        raise TtsError(str(exc)) from None


def list_voices():
    code, out, err = _powershell([
        "-Command",
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.GetInstalledVoices() | ForEach-Object {"
        " $i=$_.VoiceInfo; ($i.Name,$i.Culture.Name,$i.Gender) -join '|' };"
        "$s.Dispose()",
    ])
    if code != 0:
        raise TtsError("音声一覧の取得に失敗しました:\n%s" % err)
    voices = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            voices.append({"name": parts[0], "culture": parts[1], "gender": parts[2]})
    return voices


def _ssml(text, pitch, rate, lang):
    """ピッチ指定があるときだけ SSML にする。

    SAPI の Rate は -10..10 の整数しか受け付けないので、
    細かい速度調整はこちらの prosody rate で行う。
    """
    body = sax.escape(text)
    prosody = []
    if pitch:
        prosody.append('pitch="%s"' % pitch)
    if rate:
        prosody.append('rate="%s"' % rate)
    if prosody:
        body = "<prosody %s>%s</prosody>" % (" ".join(prosody), body)
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xml:lang="%s">%s</speak>' % (lang, body))


def synthesize(text, out_path, voice=None, rate=0, pitch=None,
               prosody_rate=None, lang="ja-JP"):
    """1 発話ぶんの WAV を作る。out_path が既にあれば何もしない。"""
    script = script_path()
    if not os.path.exists(script):
        raise TtsError("speak.ps1 が見つかりません: %s" % script)

    if pitch or prosody_rate:
        content = _ssml(text, pitch, prosody_rate, lang)
    else:
        content = text

    in_path = out_path + ".txt"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(in_path, "w", encoding="utf-8") as f:
        f.write(content)

    code, out, err = _powershell([
        "-File", script, "-InFile", in_path, "-OutFile", out_path,
        "-Voice", voice or "", "-Rate", str(int(rate)),
    ])
    if code != 0 or not os.path.exists(out_path):
        raise TtsError("音声合成に失敗しました (%s):\n%s%s" % (text[:30], out, err))
    return out_path


def cache_path(cache_dir, text, voice, rate, pitch, prosody_rate, lang):
    key = "|".join(str(x) for x in (text, voice, rate, pitch, prosody_rate, lang))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir, "tts_%s.wav" % digest)


def synthesize_cached(cache_dir, text, voice=None, rate=0, pitch=None,
                      prosody_rate=None, lang="ja-JP"):
    path = cache_path(cache_dir, text, voice, rate, pitch, prosody_rate, lang)
    if os.path.exists(path) and os.path.getsize(path) > 44:
        return path, True
    return synthesize(text, path, voice, rate, pitch, prosody_rate, lang), False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for v in list_voices():
        print("%(name)s  [%(culture)s / %(gender)s]" % v)
