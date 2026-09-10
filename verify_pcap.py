#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成した pcap を読み直して中身を検証・要約する。

Wireshark を開かずに「SIP のダイアログが破綻していないか」「RTP の
シーケンスとタイムスタンプが連続しているか」を確認するためのもの。
--extract を付けると RTP から音声を WAV に戻すので、認識対象の音が
実際にどう入っているかも耳で確かめられる。
"""

import argparse
import io
import os
import struct
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sipgen import audio


def read_pcap(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 24:
        raise ValueError("pcap として短すぎます")
    magic = struct.unpack("<I", data[:4])[0]
    if magic == 0xA1B2C3D4:
        endian, scale = "<", 1_000_000
    elif magic == 0xD4C3B2A1:
        endian, scale = ">", 1_000_000
    else:
        raise ValueError("pcap のマジックナンバーが不正です: 0x%08x" % magic)
    linktype = struct.unpack(endian + "I", data[20:24])[0]
    pos = 24
    packets = []
    while pos + 16 <= len(data):
        sec, usec, caplen, _ = struct.unpack(endian + "IIII", data[pos:pos + 16])
        pos += 16
        packets.append((sec + usec / scale, data[pos:pos + caplen]))
        pos += caplen
    return linktype, packets


def _checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def parse_udp(frame):
    """Ethernet/IPv4/UDP を剥がして (送信元, 宛先, ペイロード, 検査結果) を返す。"""
    if len(frame) < 14 or frame[12:14] != b"\x08\x00":
        return None
    ip = frame[14:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 17:
        return None
    src = ".".join(str(b) for b in ip[12:16])
    dst = ".".join(str(b) for b in ip[16:20])
    total_len = struct.unpack("!H", ip[2:4])[0]
    ip_ok = _checksum(ip[:ihl]) == 0

    udp = ip[ihl:total_len]
    sport, dport, ulen, _ = struct.unpack("!HHHH", udp[:8])
    pseudo = ip[12:20] + struct.pack("!BBH", 0, 17, ulen)
    udp_ok = _checksum(pseudo + udp) == 0
    return {"src": src, "dst": dst, "sport": sport, "dport": dport,
            "payload": udp[8:ulen], "ip_ok": ip_ok, "udp_ok": udp_ok}


def looks_like_sip(payload):
    return payload[:8].startswith(b"SIP/2.0") or b" SIP/2.0\r\n" in payload[:200]


def parse_sip(payload):
    text = payload.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = OrderedDict()
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return {"start_line": lines[0], "headers": headers, "body": body}


def parse_rtp(payload):
    if len(payload) < 12 or (payload[0] >> 6) != 2:
        return None
    cc = payload[0] & 0x0F
    marker = bool(payload[1] & 0x80)
    pt = payload[1] & 0x7F
    seq, ts, ssrc = struct.unpack("!HII", payload[2:12])
    offset = 12 + cc * 4
    return {"pt": pt, "seq": seq, "ts": ts, "ssrc": ssrc,
            "marker": marker, "payload": payload[offset:]}


def verify(path, extract_prefix=None, show_sip=True):
    linktype, packets = read_pcap(path)
    problems = []
    if linktype != 1:
        problems.append("リンク層が Ethernet ではありません (linktype=%d)" % linktype)

    sip_messages = []
    streams = OrderedDict()
    bad_ip = bad_udp = non_udp = 0
    last_ts = None

    for ts, frame in packets:
        if last_ts is not None and ts < last_ts - 1e-9:
            problems.append("タイムスタンプが逆行しています (%.6f)" % ts)
        last_ts = ts

        info = parse_udp(frame)
        if info is None:
            non_udp += 1
            continue
        if not info["ip_ok"]:
            bad_ip += 1
        if not info["udp_ok"]:
            bad_udp += 1

        if looks_like_sip(info["payload"]):
            sip_messages.append((ts, info, parse_sip(info["payload"])))
            continue
        pkt = parse_rtp(info["payload"])
        if pkt is None:
            continue
        key = (info["src"], info["sport"], info["dst"], info["dport"], pkt["ssrc"])
        streams.setdefault(key, []).append((ts, pkt))

    if bad_ip:
        problems.append("IP ヘッダのチェックサム誤り: %d 件" % bad_ip)
    if bad_udp:
        problems.append("UDP チェックサム誤り: %d 件" % bad_udp)

    print("=" * 72)
    print("pcap: %s" % path)
    print("  パケット数 %d / 期間 %.2f 秒" %
          (len(packets), (packets[-1][0] - packets[0][0]) if packets else 0.0))
    if non_udp:
        print("  UDP 以外: %d 件" % non_udp)

    _report_sip(sip_messages, problems, show_sip)
    _report_rtp(streams, problems, extract_prefix)

    print("-" * 72)
    if problems:
        print("問題あり (%d 件):" % len(problems))
        for p in problems:
            print("  ! %s" % p)
        return 1
    print("問題は見つかりませんでした。")
    return 0


def _report_sip(messages, problems, show_sip):
    print("")
    print("[SIP] %d メッセージ" % len(messages))
    if not messages:
        problems.append("SIP メッセージが 1 つもありません")
        return

    base = messages[0][0]
    call_ids = set()
    for ts, info, msg in messages:
        call_ids.update(msg["headers"].get("call-id", []))
        if show_sip:
            sdp_dir = ""
            for line in msg["body"].split("\r\n"):
                if line in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
                    sdp_dir = "  [%s]" % line[2:]
            print("  %7.3fs  %-15s -> %-15s  %-28s%s"
                  % (ts - base, info["src"], info["dst"],
                     msg["start_line"], sdp_dir))
        for required in ("call-id", "cseq", "from", "to", "via"):
            if required not in msg["headers"]:
                problems.append("%s に %s ヘッダがありません"
                                % (msg["start_line"], required))
        declared = msg["headers"].get("content-length", ["0"])[0]
        if declared.isdigit() and int(declared) != len(msg["body"].encode("utf-8")):
            problems.append("%s の Content-Length が本文長と一致しません"
                            % msg["start_line"])

    if len(call_ids) != 1:
        problems.append("Call-ID が 1 つではありません: %s" % ", ".join(call_ids))

    # ダイアログとして最低限成立しているか
    methods = [m[2]["start_line"].split()[0] for m in messages
               if not m[2]["start_line"].startswith("SIP/2.0")]
    if "INVITE" not in methods:
        problems.append("INVITE がありません")
    if "ACK" not in methods:
        problems.append("ACK がありません")
    if "BYE" not in methods:
        problems.append("BYE がありません")

    # 200 OK には To タグが必要 (ダイアログ確立の条件)
    for ts, info, msg in messages:
        if msg["start_line"].startswith("SIP/2.0 200"):
            to = msg["headers"]["to"][0]
            if ";tag=" not in to:
                problems.append("200 OK の To に tag がありません: %s" % to)


def _report_rtp(streams, problems, extract_prefix):
    print("")
    print("[RTP] %d ストリーム" % len(streams))
    if not streams:
        problems.append("RTP ストリームがありません")
        return

    for key, entries in streams.items():
        src, sport, dst, dport, ssrc = key
        pts = {}
        for _, pkt in entries:
            pts[pkt["pt"]] = pts.get(pkt["pt"], 0) + 1

        gaps = dups = 0
        expected = None
        seen = set()
        for _, pkt in entries:
            if pkt["seq"] in seen:
                dups += 1
            seen.add(pkt["seq"])
            if expected is not None and pkt["seq"] != expected:
                gaps += (pkt["seq"] - expected) & 0xFFFF
            expected = (pkt["seq"] + 1) & 0xFFFF

        audio_pkts = [p for _, p in entries if p["pt"] in (0, 8)]
        ts_problems = 0
        for a, b in zip(audio_pkts, audio_pkts[1:]):
            delta = (b["ts"] - a["ts"]) & 0xFFFFFFFF
            if delta % 160 != 0:
                ts_problems += 1
        markers = sum(1 for _, p in entries if p["marker"])
        duration = entries[-1][0] - entries[0][0]

        print("  %s:%d -> %s:%d  SSRC=0x%08x" % (src, sport, dst, dport, ssrc))
        print("      パケット %d / %.2f 秒 / PT %s / marker %d"
              % (len(entries), duration,
                 ", ".join("%d×%d" % (pt, n) for pt, n in sorted(pts.items())),
                 markers))
        if gaps or dups or ts_problems:
            print("      欠番 %d / 重複 %d / タイムスタンプ不整合 %d"
                  % (gaps, dups, ts_problems))
        if dups:
            problems.append("SSRC 0x%08x にシーケンス番号の重複があります" % ssrc)
        if ts_problems:
            problems.append("SSRC 0x%08x にタイムスタンプの不整合があります" % ssrc)

        if extract_prefix:
            _extract(extract_prefix, src, sport, audio_pkts)


def _extract(prefix, src, sport, audio_pkts):
    if not audio_pkts:
        return
    pt = audio_pkts[0]["pt"]
    table = audio._ULAW_DECODE if pt == 0 else audio._ALAW_DECODE
    samples = []
    for pkt in audio_pkts:
        samples.extend(table[b] for b in pkt["payload"])
    path = "%s_%s_%d.wav" % (prefix, src.replace(".", "-"), sport)
    audio.write_wav(path, samples)
    print("      -> %s (%.2f 秒)" % (path, len(samples) / audio.SAMPLE_RATE))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(
        description="生成した pcap の SIP/RTP を検証して要約します。")
    p.add_argument("pcap", nargs="+", help="検証する pcap ファイル")
    p.add_argument("--extract", metavar="接頭辞",
                   help="RTP から音声を WAV に戻して書き出す")
    p.add_argument("--no-sip-list", action="store_true",
                   help="SIP メッセージの一覧を省略する")
    args = p.parse_args(argv)

    code = 0
    for path in args.pcap:
        code |= verify(path, args.extract, not args.no_sip_list)
    return code


if __name__ == "__main__":
    sys.exit(main())
