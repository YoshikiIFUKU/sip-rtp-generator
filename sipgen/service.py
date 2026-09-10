"""Windows サービスの停止・開始。

生成した pcap を受け渡すために、認識サービスを「開始パラメータ付きで」
起動し直す用途を想定している。services.msc の［開始パラメーター］欄と
同じ意味を持つのは sc.exe の `sc start <名前> <引数...>` なので、
開始だけは sc.exe を使う。

状態の判定は Get-Service の Status を見る。sc.exe の出力は表示言語に
依存するが、Get-Service が返す列挙名 (Running / Stopped) は環境に
よらず一定なので、待ち合わせの判定はこちらに寄せている。
"""

import locale
import os
import subprocess
import time

# 状態が変わるのを待つあいだのポーリング間隔
POLL_SECONDS = 0.5

STOPPED = "Stopped"
RUNNING = "Running"


class ServiceError(Exception):
    pass


def _decode(raw):
    for encoding in (locale.getpreferredencoding(False), "cp932", "utf-8"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _run(cmd, timeout=60):
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise ServiceError("%s を実行できません (Windows 以外では使えません)" % cmd[0])
    except subprocess.TimeoutExpired:
        raise ServiceError("%s が %d 秒で終わりませんでした" % (cmd[0], timeout))
    return proc.returncode, _decode(proc.stdout).strip(), _decode(proc.stderr).strip()


def _powershell(script, timeout=60):
    return _run(["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script], timeout)


def status(name):
    """サービスの状態を返す。存在しなければ None。"""
    code, out, _ = _powershell(
        "$ErrorActionPreference='SilentlyContinue';"
        "(Get-Service -Name '%s').Status" % name.replace("'", "''"))
    out = out.strip()
    return out or None


def is_elevated():
    """管理者として実行されているか。サービス操作には必要。"""
    code, out, _ = _powershell(
        "([Security.Principal.WindowsPrincipal]"
        "[Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")
    return out.strip().lower() == "true"


def _wait_for(name, target, timeout):
    deadline = time.time() + timeout
    while True:
        current = status(name)
        if current == target:
            return current
        if time.time() >= deadline:
            raise ServiceError(
                "%s が %d 秒以内に %s になりませんでした (現在: %s)"
                % (name, timeout, target, current or "不明"))
        time.sleep(POLL_SECONDS)


def build_start_args(template, pcap_path):
    """開始パラメータのテンプレートを実際の引数リストにする。

    先に空白で区切ってから {pcap} を差し込むので、pcap のパスに空白が
    入っていても 1 つの引数のまま保たれる。
    """
    absolute = os.path.abspath(pcap_path)
    tokens = template.split() if isinstance(template, str) else list(template)
    return [token.replace("{pcap}", absolute) for token in tokens]


def stop(name, timeout=30, log=print):
    current = status(name)
    if current is None:
        raise ServiceError("サービスが見つかりません: %s" % name)
    if current == STOPPED:
        log("  %s は既に停止しています" % name)
        return
    log("  %s を停止しています (現在: %s)…" % (name, current))
    code, out, err = _run(["sc.exe", "stop", name], timeout=timeout)
    if code != 0:
        # 1062 = サービスは開始されていない。停止済みなら問題ない
        if code != 1062:
            raise ServiceError(_sc_failure("停止", name, code, out, err))
    _wait_for(name, STOPPED, timeout)
    log("  停止しました")


def start(name, args=(), timeout=30, log=print):
    current = status(name)
    if current is None:
        raise ServiceError("サービスが見つかりません: %s" % name)
    if current == RUNNING:
        raise ServiceError("%s は既に開始されています。先に停止してください。" % name)
    log("  %s を開始しています…" % name)
    log("    開始パラメータ: %s" % (" ".join(args) if args else "(なし)"))
    code, out, err = _run(["sc.exe", "start", name] + list(args), timeout=timeout)
    if code != 0:
        raise ServiceError(_sc_failure("開始", name, code, out, err))
    _wait_for(name, RUNNING, timeout)
    log("  開始しました")


def restart_with_pcap(name, template, pcap_path, stop_timeout=30,
                      start_timeout=30, log=print):
    """pcap を開始パラメータに埋めてサービスを入れ直す。"""
    if not os.path.exists(pcap_path):
        raise ServiceError("pcap が見つかりません: %s" % pcap_path)
    args = build_start_args(template, pcap_path)

    if not is_elevated():
        raise ServiceError(
            "サービスの操作には管理者権限が必要です。\n"
            "  管理者として実行したターミナルから、次のコマンドを実行してください:\n"
            "    sc stop %s\n"
            "    sc start %s %s" % (name, name, " ".join(args)))

    log("サービスを入れ直しています: %s" % name)
    stop(name, stop_timeout, log)
    start(name, args, start_timeout, log)
    return args


def _sc_failure(action, name, code, out, err):
    hint = {
        5: "アクセスが拒否されました。管理者としてターミナルを開き直してください。",
        1060: "そのサービスは登録されていません。",
        1062: "サービスは開始されていません。",
        1053: "サービスが時間内に応答しませんでした。開始パラメータを確認してください。",
    }.get(code)
    detail = "\n".join(x for x in (out, err) if x)
    message = "%s に失敗しました: %s (sc.exe 終了コード %d)" % (action, name, code)
    if hint:
        message += "\n  " + hint
    if detail:
        message += "\n  " + detail.replace("\n", "\n  ")
    return message
