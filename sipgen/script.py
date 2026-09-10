"""通話原稿のパース。

原稿は「誰が何を話すか」を上から順に並べただけのテキストにする。
台本を読むのと同じ順序で通話が組み立てられることを優先し、
タイミングは既定の間合いに任せ、必要なときだけ @wait で調整する。

  # 行頭の # はコメント
  agent: お電話ありがとうございます。
  customer: 契約内容を確認したいのですが。
  @wait 1.5           1.5 秒の間。負の値を書くと直前の発話に食い気味に重なる
  @hold               保留 (re-INVITE)。@hold agent のように掛ける側も書ける
  @unhold             保留解除
  @dtmf customer: 1234#   RFC 2833 の DTMF
  @wav agent: ./ivr.wav   既存の WAV をそのまま流す
  @hangup agent       この側が BYE を送る
"""

import os
import re

UTTERANCE = "utterance"
WAV = "wav"
WAIT = "wait"
HOLD = "hold"
UNHOLD = "unhold"
DTMF = "dtmf"
HANGUP = "hangup"

_SPEAKER_LINE = re.compile(r"^([^:：@]+)\s*[:：]\s*(.*)$")
_DIRECTIVE_ARG = re.compile(r"^@(\w+)\s*(.*)$")


class ScriptError(Exception):
    pass


def parse(text, speakers, base_dir="."):
    """原稿テキストをイベントのリストにする。"""
    events = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            event = _parse_line(line, speakers, base_dir)
        except ScriptError as exc:
            raise ScriptError("%d 行目: %s\n  > %s" % (lineno, exc, raw)) from None
        if event:
            event["line"] = lineno
            events.append(event)
    if not events:
        raise ScriptError("原稿に発話が 1 つもありません")
    return events


def _parse_line(line, speakers, base_dir):
    if line.startswith("@"):
        return _parse_directive(line, speakers, base_dir)

    match = _SPEAKER_LINE.match(line)
    if not match:
        raise ScriptError("『話者: セリフ』の形式か @ で始まる指示を書いてください")
    speaker = match.group(1).strip()
    text = match.group(2).strip()
    _check_speaker(speaker, speakers)
    if not text:
        raise ScriptError("セリフが空です")
    return {"type": UTTERANCE, "speaker": speaker, "text": text}


def _parse_directive(line, speakers, base_dir):
    match = _DIRECTIVE_ARG.match(line)
    if not match:
        raise ScriptError("指示の書式が不正です")
    name = match.group(1).lower()
    arg = match.group(2).strip()

    if name == "wait":
        try:
            seconds = float(arg)
        except ValueError:
            raise ScriptError("@wait には秒数を書いてください") from None
        return {"type": WAIT, "seconds": seconds}

    if name in ("hold", "unhold"):
        side = _resolve_side(arg, speakers) if arg else None
        return {"type": HOLD if name == "hold" else UNHOLD, "side": side}

    if name == "hangup":
        side = _resolve_side(arg, speakers) if arg else None
        return {"type": HANGUP, "side": side}

    if name in ("dtmf", "wav"):
        sub = _SPEAKER_LINE.match(arg)
        if not sub:
            raise ScriptError("@%s は『@%s 話者: 値』の形式で書いてください" % (name, name))
        speaker = sub.group(1).strip()
        value = sub.group(2).strip()
        _check_speaker(speaker, speakers)
        if name == "dtmf":
            invalid = [c for c in value if c not in "0123456789*#ABCD"]
            if not value or invalid:
                raise ScriptError("DTMF に使えない文字です: %s" % "".join(invalid))
            return {"type": DTMF, "speaker": speaker, "digits": value}
        path = value if os.path.isabs(value) else os.path.join(base_dir, value)
        if not os.path.exists(path):
            raise ScriptError("WAV が見つかりません: %s" % path)
        return {"type": WAV, "speaker": speaker, "path": path}

    raise ScriptError("知らない指示です: @%s" % name)


def _check_speaker(speaker, speakers):
    if speaker not in speakers:
        raise ScriptError("設定にない話者です: %s (使えるのは %s)"
                          % (speaker, " / ".join(sorted(speakers))))


def _resolve_side(arg, speakers):
    """@hold agent のような指定を local/remote に読み替える。"""
    if arg in ("local", "remote"):
        return arg
    if arg in speakers:
        return speakers[arg]["side"]
    raise ScriptError("local / remote か話者名を指定してください: %s" % arg)
