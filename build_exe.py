#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配布用の実行ファイルを作る。

  python -m pip install pyinstaller
  python build_exe.py

dist/ に 2 つの実行ファイルが出る。どちらも配布先に Python を入れずに動く。

  SIP通話ジェネレータ.exe   GUI 版（コンソールなし）
  gen_call.exe              CLI 版（バッチから呼ぶ用）

PyInstaller は「作るとき」だけ必要で、出来上がった exe の実行には要らない。

speak.ps1 だけはデータとして同梱する必要がある。1 ファイル形式だと実行時に
テンポラリへ展開されるため、tts.script_path() が sys._MEIPASS を見て
場所を解決している。
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
GUI_NAME = "SIP通話ジェネレータ"
CLI_NAME = "gen_call"

# speak.ps1 は sipgen/ の下に置かれている前提で参照されるので、
# 同梱先も sipgen にそろえる
DATA = "%s%s%s" % (os.path.join("sipgen", "speak.ps1"), os.pathsep, "sipgen")


def build(entry, name, windowed):
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--name", name,
        "--add-data", DATA,
        # GUI から検証を呼ぶため、コマンド側のモジュールも取り込む
        "--hidden-import", "verify_pcap",
        "--paths", ROOT,
    ]
    args.append("--windowed" if windowed else "--console")
    args.append(os.path.join(ROOT, entry))

    print("=" * 70)
    print("ビルド: %s  (%s)" % (name, entry))
    print("=" * 70)
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit("PyInstaller が失敗しました: %s" % name)


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyInstaller が入っていません。次のコマンドで入れてください:\n"
            "  python -m pip install pyinstaller")

    for folder in ("build", "dist"):
        shutil.rmtree(os.path.join(ROOT, folder), ignore_errors=True)

    build("sip_gui.pyw", GUI_NAME, windowed=True)
    build("gen_call.py", CLI_NAME, windowed=False)

    print()
    print("できあがりました:")
    for name in (GUI_NAME, CLI_NAME):
        path = os.path.join(ROOT, "dist", name + ".exe")
        if os.path.exists(path):
            size = os.path.getsize(path) / 1024 / 1024
            print("  %s  (%.1f MB)" % (path, size))
    print()
    print("配布するのは dist/ の exe だけで足ります。")
    print("サービスの停止・開始を使う場合は、管理者として実行してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
