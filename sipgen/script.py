"""通話台本のパース。

書式は既存の台本ツールにそろえてある。1 行 1 発話の
「話者：本文」だけで台本になり、話者名は自由、事前の定義も要らない。

  # 行頭の # はコメント。空行は無視
  OP：お電話ありがとうございます、サポートセンターでございます。
  CU：料金プランのことで確認したいのですが。

区切りは既定で全角「：」と半角「:」の両方。`separators` で変えられ、
`pattern` に正規表現を渡せば「[OP] 本文」のような書式も読める
(1 番目のグループが話者、2 番目が本文)。

これに加えて、SIP のテスト通話に要る指示だけを丸括弧の行で書ける。
発話行と見分けがつくよう、行全体が括弧で囲まれているものだけを
指示として扱う。

  （3秒あける）           間を空ける
  （0.6秒かぶせる）       次の発話を直前に食い込ませる
  （保留）                保留する。（保留解除）まで
  （保留 6秒）            6 秒だけ保留する
  （保留 OP）             掛ける側を指定する
  （保留解除）            保留を解除する
  （プッシュ音 CU：1234#）DTMF を送る
  （音声 OP：./ivr.wav）  既存の WAV を流す
  （切断 OP）             この側から BYE を送る

以前の @hold / @wait 形式の台本もそのまま読める。
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

DEFAULT_SEPARATORS = ":："

# 本文中のコロンを話者と読み違えないための上限。既存の台本ツールと同じ
MAX_SPEAKER_LENGTH = 24

# 行全体を囲む括弧。全角・半角のどちらでも書ける
_BRACKETED = re.compile(r"^[（(]\s*(.+?)\s*[）)]$")
_DIRECTIVE_AT = re.compile(r"^@(\w+)\s*(.*)$")
_TRAILING_COMMENT = re.compile(r"\s+#.*$")

# 秒数。単位を必須にしているのは、CU2 のように数字を含む話者名を
# 秒数と読み違えないため。単位なしで書けるのは「（3）」のように
# 括弧の中が数字だけのときに限る (_only_time)
_SECONDS = r"(-?\d+(?:\.\d+)?)\s*(?:秒|sec|s)(?![A-Za-z])"
_BARE_NUMBER = r"\s*(-?\d+(?:\.\d+)?)\s*"
_WAIT_WORDS = ("あける", "空ける", "待つ", "待機", "無音", "ポーズ", "wait", "pause")
_OVERLAP_WORDS = ("かぶせ", "被せ", "重ね", "食い気味", "overlap")
_HOLD_WORDS = ("保留", "hold")
_UNHOLD_WORDS = ("保留解除", "解除", "unhold", "resume")
_DTMF_WORDS = ("プッシュ音", "プッシュ", "押下", "dtmf", "トーン")
_WAV_WORDS = ("音声", "wav", "ファイル")
_HANGUP_WORDS = ("切断", "終話", "切る", "hangup", "bye")

DEFAULT_OVERLAP = 0.5     # （かぶせる）に秒数がないときの重なり


class ScriptError(Exception):
    pass


class ParsedScript:
    """台本の解析結果。

    speakers は出現順の話者名。どちら側の RTP に乗せるかは
    speakers.resolve() が名前から決めるので、ここでは並びだけ持つ。
    """

    def __init__(self, events, speakers):
        self.events = events
        self.speakers = speakers

    def __len__(self):
        return len(self.events)

    def __iter__(self):
        return iter(self.events)


def line_regex(separators=DEFAULT_SEPARATORS, pattern=None):
    if pattern:
        return re.compile(pattern)
    if not separators:
        separators = DEFAULT_SEPARATORS
    cls = "".join("\\" + c if c in "\\]^-[" else c for c in separators)
    return re.compile(r"^\s*([^%s\r\n]{1,%d})\s*[%s]\s*(.*)$"
                      % (cls, MAX_SPEAKER_LENGTH, cls))


def format_hint(separators=DEFAULT_SEPARATORS, pattern=None):
    if pattern:
        return "正規表現 '%s'" % pattern
    sample = (separators or DEFAULT_SEPARATORS)[0]
    return "『話者%s本文』の形式（区切り文字: %s）" % (sample, separators or DEFAULT_SEPARATORS)


def parse(text, base_dir=".", separators=DEFAULT_SEPARATORS, pattern=None,
          speakers=None):
    """台本テキストを解析する。

    speakers を渡すと、そこにない話者名をエラーにする。省略した場合は
    出てきた名前をそのまま受け入れる (こちらが既定の使い方)。
    """
    regex = line_regex(separators, pattern)
    events = []
    found = []

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        try:
            event = _parse_line(line, regex, base_dir, separators, pattern)
        except ScriptError as exc:
            raise ScriptError("%d 行目: %s\n  > %s" % (lineno, exc, raw.strip())) from None
        if not event:
            continue
        event["line"] = lineno
        name = event.get("speaker")
        if name:
            if speakers is not None and not _known(name, speakers):
                raise ScriptError(
                    "%d 行目: 設定にない話者です: %s（使えるのは %s）\n  > %s"
                    % (lineno, name, " / ".join(speakers), raw.strip()))
            if name not in found:
                found.append(name)
        events.append(event)

    if not events:
        raise ScriptError("台本に発話が 1 つもありません")
    if not any(e["type"] in (UTTERANCE, WAV) for e in events):
        raise ScriptError("台本に発話が 1 つもありません（指示行だけになっています）")
    return ParsedScript(events, found)


def _known(name, speakers):
    from . import speakers as speakers_mod
    key = speakers_mod.normalize(name)
    return any(speakers_mod.normalize(k) == key for k in speakers)


def _strip_comment(raw):
    """コメントを落とした 1 行を返す。全部コメントなら空文字。

    行末コメントを認めるのは指示行だけにしている。発話行は読み上げる
    内容そのものなので、「CU：#1番でお願いします」のような文を
    勝手に削らない。
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return ""
    # 行末コメントを削る前の行は「（…）  # コメント」の形なので、
    # 閉じ括弧まで含めて一致する _BRACKETED では判定できない。
    # 開き括弧で始まるかどうかだけを見る
    if line.startswith("@") or line[0] in "（(":
        return _TRAILING_COMMENT.sub("", line).strip()
    return line


def _parse_line(line, regex, base_dir, separators, pattern):
    # 指示行は発話行より先に見る。「（プッシュ音 CU：1234#）」のように
    # 中にコロンを含む指示があるため
    bracketed = _BRACKETED.match(line)
    if bracketed:
        return _parse_directive(bracketed.group(1), base_dir)
    if line.startswith("@"):
        return _parse_at_directive(line, base_dir)

    match = regex.match(line)
    if not match:
        raise ScriptError("%s に一致しません" % format_hint(separators, pattern))
    speaker = match.group(1).strip()
    body = match.group(2).strip()
    if not speaker:
        raise ScriptError("話者名が空です")
    if not body:
        return None          # 「OP：」だけの行は読み飛ばす
    return {"type": UTTERANCE, "speaker": speaker, "text": body}


# ----------------------------------------------------------------------
# （…）形式の指示
# ----------------------------------------------------------------------
def _parse_directive(body, base_dir):
    body = body.strip()
    if not body:
        raise ScriptError("括弧の中が空です")

    lowered = body.lower()

    # 「保留解除」は「保留」より先に判定する（前方一致で食われるため）
    if _has_word(lowered, _UNHOLD_WORDS):
        return {"type": UNHOLD, "speaker": None}

    if _has_word(lowered, _HOLD_WORDS):
        return _parse_hold(body)

    if _has_word(lowered, _DTMF_WORDS):
        return _parse_pair(body, _DTMF_WORDS, DTMF, base_dir)

    if _has_word(lowered, _WAV_WORDS):
        return _parse_pair(body, _WAV_WORDS, WAV, base_dir)

    if _has_word(lowered, _HANGUP_WORDS):
        return {"type": HANGUP, "speaker": _strip_words(body, _HANGUP_WORDS) or None}

    if _has_word(lowered, _OVERLAP_WORDS):
        seconds = _find_seconds(body)
        return {"type": WAIT, "seconds": -(seconds if seconds is not None
                                           else DEFAULT_OVERLAP)}

    # 「3秒あける」「3秒」「3」なら間を空ける指示
    if _only_time(body):
        return {"type": WAIT, "seconds": float(re.search(
            r"-?\d+(?:\.\d+)?", body).group(0))}
    seconds = _find_seconds(body)
    if seconds is not None and _has_word(lowered, _WAIT_WORDS):
        return {"type": WAIT, "seconds": seconds}

    raise ScriptError(
        "指示として解釈できません: %s\n"
        "     使えるのは （3秒あける）（0.6秒かぶせる）（保留）（保留 6秒）"
        "（保留解除）（プッシュ音 CU：1234#）（音声 OP：a.wav）（切断 OP）" % body)


def _parse_hold(body):
    seconds = _find_seconds(body)
    speaker = _strip_words(_remove_time(body), _HOLD_WORDS)
    event = {"type": HOLD, "speaker": speaker or None}
    if seconds is not None:
        if seconds <= 0:
            raise ScriptError("保留の秒数は正の値で書いてください: %s" % body)
        event["seconds"] = seconds
    return event


def _parse_pair(body, words, kind, base_dir):
    """「プッシュ音 CU：1234#」のような『指示語 話者：値』を分解する。"""
    rest = _strip_words(body, words)
    match = re.match(r"^(.{1,%d}?)\s*[:：]\s*(.+)$" % MAX_SPEAKER_LENGTH, rest)
    if not match:
        label = "プッシュ音" if kind == DTMF else "音声"
        raise ScriptError("『%s 話者：値』の形式で書いてください: %s" % (label, body))
    speaker = match.group(1).strip()
    value = match.group(2).strip()
    if not speaker:
        raise ScriptError("話者名が空です: %s" % body)

    if kind == DTMF:
        return _make_dtmf(speaker, value)
    return _make_wav(speaker, value, base_dir)


def _make_dtmf(speaker, value):
    invalid = [c for c in value if c not in "0123456789*#ABCD"]
    if not value or invalid:
        raise ScriptError("DTMF に使えない文字です: %s" % "".join(invalid))
    return {"type": DTMF, "speaker": speaker, "digits": value}


def _make_wav(speaker, value, base_dir):
    path = value if os.path.isabs(value) else os.path.join(base_dir, value)
    if not os.path.exists(path):
        raise ScriptError("WAV が見つかりません: %s" % path)
    return {"type": WAV, "speaker": speaker, "path": path}


def _has_word(lowered, words):
    return any(word.lower() in lowered for word in words)


def _strip_words(body, words):
    out = body
    for word in sorted(words, key=len, reverse=True):
        out = re.sub(re.escape(word), " ", out, flags=re.IGNORECASE)
    # 「OP から切る」「OP で保留」のような助詞も落とす
    out = re.sub(r"[はがをにでのからより、。\s]+", " ", out)
    return out.strip()


def _find_seconds(body):
    match = re.search(_SECONDS, body)
    return float(match.group(1)) if match else None


def _remove_time(body):
    return re.sub(_SECONDS, " ", body, count=1)


def _only_time(body):
    return (re.fullmatch(r"\s*%s\s*" % _SECONDS, body) is not None
            or re.fullmatch(_BARE_NUMBER, body) is not None)


# ----------------------------------------------------------------------
# 旧書式（@hold など）。以前の台本をそのまま読めるようにしておく
# ----------------------------------------------------------------------
def _parse_at_directive(line, base_dir):
    match = _DIRECTIVE_AT.match(line)
    if not match:
        raise ScriptError("指示の書式が不正です")
    name = match.group(1).lower()
    arg = match.group(2).strip()

    if name == "wait":
        try:
            return {"type": WAIT, "seconds": float(arg)}
        except ValueError:
            raise ScriptError("@wait には秒数を書いてください") from None

    if name in ("hold", "unhold"):
        return {"type": HOLD if name == "hold" else UNHOLD,
                "speaker": arg or None}

    if name == "hangup":
        return {"type": HANGUP, "speaker": arg or None}

    if name in ("dtmf", "wav"):
        sub = re.match(r"^(.{1,%d}?)\s*[:：]\s*(.+)$" % MAX_SPEAKER_LENGTH, arg)
        if not sub:
            raise ScriptError("@%s は『@%s 話者: 値』の形式で書いてください"
                              % (name, name))
        speaker, value = sub.group(1).strip(), sub.group(2).strip()
        if name == "dtmf":
            return _make_dtmf(speaker, value)
        return _make_wav(speaker, value, base_dir)

    raise ScriptError("知らない指示です: @%s" % name)
