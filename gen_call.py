#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通話原稿から SIP + RTP の pcap を作るコマンドライン。

  python gen_call.py examples/inbound.txt -c examples/config.json -o out/call.pcap

原稿の書き方は README.md と examples/ を参照。
"""

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sipgen import audio, builder, config, loader, script, service, tabular, tts


def main(argv=None):
    _force_utf8_console()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list_voices:
        return _list_voices()
    if args.init_config:
        config.write_example(args.init_config)
        print("設定ファイルのひな形を書き出しました: %s" % args.init_config)
        return 0
    if not args.script:
        parser.error("原稿ファイルを指定してください (--list-voices / --init-config を除く)")

    try:
        cfg = config.load(args.config, _cli_overrides(args))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return _fail("設定の読み込みに失敗しました: %s" % exc)

    if args.show_columns:
        return _show_columns(args)

    base_dir = os.path.dirname(os.path.abspath(args.script)) or "."
    try:
        parsed = loader.parse_file(args.script, cfg, _table_options(args))
        resolved = loader.resolve_speakers(cfg, parsed)
    except (script.ScriptError, tabular.TableError, loader.LoadError) as exc:
        return _fail("台本の書式エラー\n%s" % exc)

    log = (lambda msg: None) if args.quiet else (lambda msg: print(msg))
    cache_dir = args.cache_dir or os.path.join(base_dir, ".tts-cache")

    log("台本: %s（%d 行）" % (args.script, len(parsed)))
    log("話者: %s" % loader.describe_speakers(resolved, parsed.speakers))
    call = builder.CallBuilder(cfg, parsed, base_dir=base_dir,
                               cache_dir=cache_dir, seed=args.seed, log=log)
    try:
        writer, transcript, plan = call.build()
    except (builder.BuildError, tts.TtsError, ValueError, OSError) as exc:
        return _fail(str(exc))

    out_path = args.output or os.path.splitext(args.script)[0] + ".pcap"
    _ensure_dir(out_path)
    count = writer.write(out_path)

    transcript_path = args.transcript or os.path.splitext(out_path)[0] + ".json"
    if transcript_path.lower() != "none":
        _ensure_dir(transcript_path)
        with io.open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False, indent=2)

    if args.export_wav:
        _export_wav(call, args.export_wav)

    _report(out_path, transcript_path, count, transcript, cfg, args)

    # pcap ができてから、認識側に取り込ませる
    if cfg["service"]["enabled"]:
        return _restart_service(cfg["service"], out_path, args.quiet)
    return 0


def _build_parser():
    p = argparse.ArgumentParser(
        prog="gen_call.py",
        description="通話原稿から SIP/RTP のテスト用 pcap を生成します。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例:
  python gen_call.py examples/inbound.txt -c examples/config.json
  python gen_call.py script.txt --server-ip 10.0.0.1 --client-ip 10.0.0.50 \\
      --from 0312345678 --to 1001 -o out/test.pcap
  python gen_call.py --list-voices
""")
    p.add_argument("script", nargs="?",
                   help="通話台本（.txt の『話者：本文』か、.csv / .tsv）")
    p.add_argument("-c", "--config", help="設定 JSON")
    p.add_argument("-o", "--output", help="出力する pcap (既定: 原稿と同名 .pcap)")
    p.add_argument("--transcript",
                   help="正解データ JSON の出力先。none で出力しない")
    p.add_argument("--export-wav", metavar="接頭辞",
                   help="RTP に載せた音を確認用 WAV として書き出す")
    p.add_argument("--cache-dir", help="TTS キャッシュの場所")
    p.add_argument("--seed", type=int,
                   help="乱数の種。同じ値なら Call-ID や SSRC が再現される")
    p.add_argument("-q", "--quiet", action="store_true", help="進捗を表示しない")

    g = p.add_argument_group("設定の上書き")
    g.add_argument("--server-ip", help="SIP サーバの IP")
    g.add_argument("--client-ip", help="クライアント電話機の IP")
    g.add_argument("--from", dest="from_user", help="From ヘッダのユーザ部")
    g.add_argument("--to", dest="to_user", help="To ヘッダのユーザ部")
    g.add_argument("--direction", choices=["inbound", "outbound"],
                   help="inbound=着信 / outbound=発信")
    g.add_argument("--codec", choices=["PCMU", "PCMA"], help="音声コーデック")
    g.add_argument("--start-time", help="pcap の開始時刻 (ISO8601)")

    t = p.add_argument_group("台本の読み取り")
    t.add_argument("--separators", metavar="文字",
                   help="『話者：本文』の区切り文字を並べて指定 (既定: :：)")
    t.add_argument("--pattern", metavar="正規表現",
                   help=r"話者プレフィックスの正規表現。"
                        r"1 番目が話者、2 番目が本文 (例: ^\[(.+?)\]\s*(.*)$)")
    t.add_argument("--delimiter", metavar="文字",
                   help="CSV/TSV の列の区切り。省略すると自動判定")
    t.add_argument("--speaker-column", metavar="列",
                   help="話者の列。列名でも 1 のような列番号でも指定できる")
    t.add_argument("--text-column", metavar="列", help="本文の列")
    t.add_argument("--start-column", metavar="列", help="開始時間の列")
    t.add_argument("--no-header", action="store_true",
                   help="CSV の先頭行を見出しではなくデータとして扱う")
    t.add_argument("--no-timings", action="store_true",
                   help="CSV の開始時間列を使わず、順番に並べる")
    t.add_argument("--show-columns", action="store_true",
                   help="区切り文字の判定結果と列一覧を表示して終了")

    h = p.add_argument_group("SIP ヘッダの追加")
    h.add_argument("--ack-header", metavar="ヘッダ", action="append", default=[],
                   help="ACK に足す・差し替えるヘッダ。"
                        '"X-Call-Id: {call_id}" のように書く。複数回指定できる')
    h.add_argument("--sip-header", metavar="対象:ヘッダ", action="append",
                   default=[],
                   help="ACK 以外にも足す場合。"
                        '"INVITE:X-Foo: bar" や "*:X-Run: 001" のように、'
                        "先頭にメソッド名か応答コード（* は全部）を書く")

    s = p.add_argument_group("音声認識サービスへの取り込み")
    s.add_argument("--restart-service", action="store_true",
                   help="pcap を生成したあとサービスを停止し、"
                        "開始パラメータに pcap のパスを付けて開始する")
    s.add_argument("--service-name", metavar="名前",
                   help="対象のサービス名 (既定: %s)"
                        % config.DEFAULTS["service"]["name"])
    # 値自体が - で始まるため、--service-args=... の形で渡してもらう
    s.add_argument("--service-args", metavar="パラメータ",
                   help='開始パラメータ。{pcap} が pcap の絶対パスになる。'
                        '値が - で始まるので --service-args="..." のように = で'
                        'つなぐこと (既定: %s)'
                        % config.DEFAULTS["service"]["start_args"])
    s.add_argument("--service-timeout", type=int, metavar="秒",
                   help="停止・開始の待ち時間 (既定: 30)")

    m = p.add_argument_group("補助")
    m.add_argument("--list-voices", action="store_true",
                   help="この PC で使える音声合成の声を一覧表示")
    m.add_argument("--init-config", metavar="PATH",
                   help="設定 JSON のひな形を書き出して終了")
    return p


def _cli_overrides(args):
    over = {}
    if args.server_ip:
        over.setdefault("sip_server", {})["ip"] = args.server_ip
    if args.client_ip:
        over.setdefault("client", {})["ip"] = args.client_ip
    if args.from_user:
        over["from"] = {"user": args.from_user}
    if args.to_user:
        over["to"] = {"user": args.to_user}
    if args.direction:
        over["direction"] = args.direction
    if args.codec:
        over["codec"] = args.codec
    if args.start_time:
        over["call"] = {"start_time": args.start_time}
    if args.separators or args.pattern:
        over["script"] = {}
        if args.separators:
            over["script"]["separators"] = args.separators
        if args.pattern:
            over["script"]["pattern"] = args.pattern

    headers = _header_overrides(args)
    if headers:
        over["sip_headers"] = headers

    svc = {}
    if args.restart_service:
        svc["enabled"] = True
    if args.service_name:
        svc["name"] = args.service_name
    if args.service_args:
        svc["start_args"] = args.service_args
    if args.service_timeout:
        svc["stop_timeout"] = svc["start_timeout"] = args.service_timeout
    if svc:
        over["service"] = svc
    return over


def _header_overrides(args):
    """--ack-header / --sip-header を sip_headers の形に直す。"""
    headers = {}
    for line in args.ack_header:
        headers.setdefault("ACK", []).append(line)
    for line in args.sip_header:
        target, sep, rest = line.partition(":")
        if not sep or ":" not in rest:
            raise ValueError(
                "--sip-header は『対象:ヘッダ名: 値』の形で書いてください: %s"
                % line)
        headers.setdefault(target.strip().upper(), []).append(rest.strip())
    return headers


def _table_options(args):
    options = {"delimiter": args.delimiter,
               "speaker_column": args.speaker_column,
               "text_column": args.text_column,
               "start_column": args.start_column}
    if args.no_header:
        options["has_header"] = False
    if args.no_timings:
        options["use_timings"] = False
    return {k: v for k, v in options.items() if v is not None}


def _show_columns(args):
    """CSV の列名が分からないときに、まず中身を見るための出口。"""
    try:
        text = loader.read_text(args.script)
        print(tabular.describe(text, args.delimiter))
    except (tabular.TableError, OSError) as exc:
        return _fail(str(exc))
    return 0


def _restart_service(svc, pcap_path, quiet):
    log = (lambda msg: None) if quiet else (lambda msg: print(msg))
    log("")
    try:
        args = service.restart_with_pcap(
            svc["name"], svc["start_args"], pcap_path,
            stop_timeout=svc["stop_timeout"], start_timeout=svc["start_timeout"],
            log=log)
    except service.ServiceError as exc:
        return _fail(str(exc))
    if not quiet:
        print("認識サービスに取り込ませました: %s %s" % (svc["name"], " ".join(args)))
    return 0


def _export_wav(call, prefix):
    _ensure_dir(prefix)
    for side, samples in call.rendered.items():
        path = "%s_%s.wav" % (prefix, side)
        audio.write_wav(path, samples)
        print("確認用 WAV: %s" % path)


def _report(out_path, transcript_path, count, transcript, cfg, args):
    if args.quiet:
        return
    info = transcript["call"]
    stats = transcript["stats"]
    print("")
    print("pcap を書き出しました: %s" % out_path)
    print("  Call-ID    : %s" % info["call_id"])
    print("  向き       : %s  (%s → %s)"
          % (info["direction"], info["from"], info["to"]))
    print("  コーデック : %s / %dms  RTP %s:%d ⇄ %s:%d"
          % (info["codec"], info["ptime"],
             transcript["streams"]["local"]["ip"],
             transcript["streams"]["local"]["port"],
             transcript["streams"]["remote"]["ip"],
             transcript["streams"]["remote"]["port"]))
    print("  通話長     : %.1f 秒 (うち音声 %.1f 秒)"
          % (info["call_duration"], info["media_duration"]))
    print("  パケット   : 合計 %d (SIP %d / RTP %d / DTMF %d)"
          % (count, len(transcript["sip"]), stats["rtp_packets"],
             stats["dtmf_packets"]))
    if stats["dropped"] or stats["suppressed"]:
        print("             ロス %d / 無音抑止 %d"
              % (stats["dropped"], stats["suppressed"]))
    holds = [e for e in transcript["events"] if e["type"] == "hold"]
    if holds:
        print("  保留       : %d 回 (%s)"
              % (len(holds), ", ".join("%.1f-%.1f秒" % (h["start"], h["end"])
                                       for h in holds)))
    if transcript_path.lower() != "none":
        print("  正解データ : %s" % transcript_path)


def _list_voices():
    try:
        voices = tts.list_voices()
    except tts.TtsError as exc:
        return _fail(str(exc))
    if not voices:
        return _fail("使える音声合成の声が見つかりませんでした。")
    print("この PC で使える声:")
    for v in voices:
        print("  %-32s %s / %s" % (v["name"], v["culture"], v["gender"]))
    print("\n設定では部分一致で指定できます (例: \"voice\": \"Haruka\")")
    return 0


def _ensure_dir(path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)


def _force_utf8_console():
    """Windows のコンソールでも日本語が化けないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _fail(message):
    print("エラー: %s" % message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
