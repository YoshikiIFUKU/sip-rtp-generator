#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GUI の起動口。

拡張子を .pyw にしてあるので、ダブルクリックしてもコンソールが開かない。
コマンドラインから起動したい場合は `python sip_gui.pyw` でよい。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sipgen import gui


def main():
    try:
        return gui.main()
    except Exception:
        # GUI が立ち上がる前に落ちるとコンソールがなく原因が分からないので、
        # ダイアログで出す
        import traceback
        detail = traceback.format_exc()
        try:
            import tkinter.messagebox as messagebox
            messagebox.showerror("SIP/RTP テスト通話ジェネレータ",
                                 "起動できませんでした:\n\n%s" % detail)
        except Exception:
            sys.stderr.write(detail)
        return 1


if __name__ == "__main__":
    sys.exit(main())
