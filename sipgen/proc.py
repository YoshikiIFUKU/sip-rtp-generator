"""外部コマンド (PowerShell / sc.exe) の呼び出し。

GUI を実行ファイルにして配布すると、コンソールを持たないプロセスから
外部コマンドを起動することになる。そのままだと呼び出しのたびに黒い窓が
一瞬開いてしまうので、CREATE_NO_WINDOW を必ず付ける。

出力の文字コードもここで吸収する。sc.exe などの表示は環境の
コードページ (日本語 Windows なら CP932) なので、UTF-8 決め打ちだと
エラーメッセージが読めなくなる。
"""

import locale
import subprocess
import sys

# Windows でコンソール窓を出さずに子プロセスを起動するフラグ
CREATE_NO_WINDOW = 0x08000000


def _creation_flags():
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


def decode(raw):
    for encoding in (locale.getpreferredencoding(False), "cp932", "utf-8"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class ProcessError(Exception):
    pass


def run(cmd, timeout=60):
    """(終了コード, 標準出力, 標準エラー) を返す。出力は復号済み。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=_creation_flags())
    except FileNotFoundError:
        raise ProcessError("%s を実行できません (Windows 以外では使えません)" % cmd[0])
    except subprocess.TimeoutExpired:
        raise ProcessError("%s が %d 秒で終わりませんでした" % (cmd[0], timeout))
    return proc.returncode, decode(proc.stdout).strip(), decode(proc.stderr).strip()


def powershell(args, timeout=60):
    return run(["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass"] + list(args), timeout)
