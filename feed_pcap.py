#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""既にある pcap を音声認識サービスに取り込ませる。

サービスを停止し、開始パラメータに pcap の絶対パスを付けて開始し直す。
gen_call.py の --restart-service と同じ処理を、生成し直さずに単体で
実行したいとき (同じ pcap をもう一度流したいときなど) に使う。

  python feed_pcap.py out/inbound.pcap

サービスの操作には管理者権限が必要なので、管理者として開いた
ターミナルから実行すること。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sipgen import config, service


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    defaults = config.DEFAULTS["service"]
    p = argparse.ArgumentParser(
        prog="feed_pcap.py",
        description="pcap を音声認識サービスに取り込ませます "
                    "(サービスを停止し、開始パラメータ付きで開始し直します)。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例:
  python feed_pcap.py out/inbound.pcap
  python feed_pcap.py out/inbound.pcap --name MyRecorder \\
      --args="--callid-generate --packet-sync {pcap}"
  python feed_pcap.py --status
""")
    p.add_argument("pcap", nargs="?", help="取り込ませる pcap")
    p.add_argument("--name", default=defaults["name"],
                   help="サービス名 (既定: %(default)s)")
    # 値自体が - で始まるため、--args=... の形で渡してもらう
    p.add_argument("--args", default=defaults["start_args"],
                   help='開始パラメータ。{pcap} が pcap の絶対パスになる。'
                        '値が - で始まるので --args="..." のように = でつなぐこと '
                        '(既定: %(default)s)')
    p.add_argument("--timeout", type=int, default=defaults["stop_timeout"],
                   help="停止・開始の待ち時間 (既定: %(default)s 秒)")
    p.add_argument("--status", action="store_true",
                   help="サービスの状態を表示して終了する")
    p.add_argument("--dry-run", action="store_true",
                   help="実行せず、発行するコマンドだけを表示する")
    args = p.parse_args(argv)

    if args.status:
        current = service.status(args.name)
        if current is None:
            print("サービスが見つかりません: %s" % args.name, file=sys.stderr)
            return 1
        print("%s: %s" % (args.name, current))
        return 0

    if not args.pcap:
        p.error("pcap を指定してください (--status / --dry-run を除く)")
    if not os.path.exists(args.pcap):
        print("エラー: pcap が見つかりません: %s" % args.pcap, file=sys.stderr)
        return 1

    start_args = service.build_start_args(args.args, args.pcap)
    if args.dry_run:
        print("sc stop %s" % args.name)
        print("sc start %s %s" % (args.name, " ".join(start_args)))
        return 0

    try:
        service.restart_with_pcap(args.name, args.args, args.pcap,
                                  stop_timeout=args.timeout,
                                  start_timeout=args.timeout)
    except service.ServiceError as exc:
        print("エラー: %s" % exc, file=sys.stderr)
        return 1
    print("取り込ませました: %s %s" % (args.name, " ".join(start_args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
