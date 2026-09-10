"""CSV / TSV の台本を読む。

列名の候補と時刻の書式は TTSWavGenerator に合わせてある。向こうで作った
台本や、認識結果を書き出した CSV（`発言内容(認識結果)` などの列を持つもの）
をそのまま渡せるようにするため。

開始時間の列があれば、その時刻に発話を置く。無ければ順に並べる。
"""

import csv
import io
import re

from . import script

# 列名の推測に使う候補。前が優先。TTSWavGenerator と同じ並び
SPEAKER_COLUMNS = ("音声のチャンネル種類", "話者", "チャンネル", "区分",
                   "speaker", "channel", "ch", "role")
TEXT_COLUMNS = ("発言内容(認識結果)", "発言内容", "発話内容", "テキスト", "内容",
                "本文", "text", "utterance", "transcript")
START_COLUMNS = ("開始時間(最新版数)", "開始時間", "開始", "start", "start_time", "begin")

DELIMITERS = (",", "\t", ";", "|")


class TableError(Exception):
    pass


def looks_tabular(path):
    return path.lower().endswith((".csv", ".tsv", ".txt")) is False or \
        path.lower().endswith((".csv", ".tsv"))


def guess_delimiter(text):
    """1 行あたりの出現数が最も安定している区切り文字を選ぶ。"""
    lines = [l for l in text.splitlines() if l.strip()][:20]
    if not lines:
        return ","
    best, best_score = ",", -1
    for delimiter in DELIMITERS:
        counts = [l.count(delimiter) for l in lines]
        if not counts or max(counts) == 0:
            continue
        # 全行で同じ数だけ出てくるものほど区切りらしい
        consistent = sum(1 for c in counts if c == counts[0])
        score = consistent * 10 + counts[0]
        if score > best_score:
            best, best_score = delimiter, score
    return best


def read_rows(text, delimiter=None):
    delimiter = delimiter or guess_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [row for row in reader if any(cell.strip() for cell in row)], delimiter


def _find_column(header, candidates):
    lowered = [(h or "").strip().lower() for h in header]
    for candidate in candidates:
        target = candidate.strip().lower()
        for i, name in enumerate(lowered):
            if name == target:
                return i
    for candidate in candidates:
        target = candidate.strip().lower()
        for i, name in enumerate(lowered):
            if name and target in name:
                return i
    return None


def guess_columns(header):
    return {
        "speaker": _find_column(header, SPEAKER_COLUMNS),
        "text": _find_column(header, TEXT_COLUMNS),
        "start": _find_column(header, START_COLUMNS),
    }


def looks_like_header(row):
    """先頭行が見出しかどうか。既知の列名に当たれば見出しとみなす。

    本文の列だけは必ず要る。話者や開始時間の列が無い 1 列だけの
    ファイルもあるので、その場合は本文の列名だけで見出しと判断する。
    """
    guess = guess_columns(row)
    if guess["text"] is None:
        return False
    return (guess["speaker"] is not None or guess["start"] is not None
            or len(row) == 1)


def parse_time(value):
    """'00:03.0' / '0:01:02.5' / '3.2' を秒にする。読めなければ None。"""
    text = (value or "").strip()
    if not text:
        return None
    seconds = 0.0
    for part in text.split(":"):
        try:
            seconds = seconds * 60 + float(part)
        except ValueError:
            return None
    return seconds


def describe(text, delimiter=None):
    """区切り文字と列の割り当てを説明する（--show-columns 用）。"""
    rows, delimiter = read_rows(text, delimiter)
    if not rows:
        raise TableError("行がありません")
    header = rows[0]
    has_header = looks_like_header(header)
    guess = guess_columns(header) if has_header else {}
    roles = {v: k for k, v in guess.items() if v is not None}
    names = {"speaker": "話者", "text": "本文", "start": "開始時間"}

    lines = ["区切り文字: %s" % {",": "カンマ", "\t": "タブ", ";": "セミコロン",
                                "|": "縦棒"}.get(delimiter, repr(delimiter)),
             "先頭行: %s" % ("見出し" if has_header else "データ"),
             "列一覧:"]
    for i, name in enumerate(header):
        role = roles.get(i)
        suffix = "  <- %s として自動判定" % names[role] if role else ""
        lines.append("  %d 列目: %s%s" % (i + 1, name, suffix))
    return "\n".join(lines)


def parse(text, base_dir=".", delimiter=None, has_header=None,
          speaker_column=None, text_column=None, start_column=None,
          use_timings=True, separators=script.DEFAULT_SEPARATORS):
    """CSV / TSV を ParsedScript にする。

    列は名前でも 1 始まりの番号でも指定できる。省略すると見出しから
    推測する。1 列に「話者：本文」がまとまっている場合は、本文列だけを
    指定すればそのまま解釈する。
    """
    rows, delimiter = read_rows(text, delimiter)
    if not rows:
        raise TableError("行がありません")

    header = rows[0]
    if has_header is None:
        has_header = looks_like_header(header)
    body = rows[1:] if has_header else rows

    columns = guess_columns(header) if has_header else {}
    speaker_idx = _resolve_column(speaker_column, header, columns.get("speaker"),
                                  has_header)
    text_idx = _resolve_column(text_column, header, columns.get("text"), has_header)
    start_idx = _resolve_column(start_column, header, columns.get("start"),
                                has_header)

    if text_idx is None:
        # 見出しが無い 2〜3 列のファイルは、並び順から素直に決める
        width = max(len(r) for r in body) if body else 0
        if width >= 3 and speaker_idx is None:
            speaker_idx, start_idx, text_idx = 0, 1, 2
        elif width == 2:
            speaker_idx, text_idx = 0, 1
        elif width == 1:
            text_idx = 0
        else:
            raise TableError(
                "本文の列が分かりません。--text-column で指定してください。\n"
                + describe(text, delimiter))

    line_regex = script.line_regex(separators)
    events = []
    found = []

    for offset, row in enumerate(body):
        lineno = offset + (2 if has_header else 1)
        cell = _cell(row, text_idx)
        if not cell:
            continue

        speaker = _cell(row, speaker_idx)
        if not speaker:
            # 話者列が無ければ「話者：本文」が 1 列に入っているとみなす
            match = line_regex.match(cell)
            if not match:
                raise TableError(
                    "%d 行目: 話者が分かりません。--speaker-column で列を指定するか、"
                    "『話者：本文』の形で書いてください -> %s" % (lineno, cell))
            speaker, cell = match.group(1).strip(), match.group(2).strip()
            if not cell:
                continue

        event = {"type": script.UTTERANCE, "speaker": speaker, "text": cell,
                 "line": lineno}
        if use_timings and start_idx is not None:
            start = parse_time(_cell(row, start_idx))
            if start is not None:
                event["start"] = start
        events.append(event)
        if speaker not in found:
            found.append(speaker)

    if not events:
        raise TableError("発話が 1 つもありません")
    return script.ParsedScript(events, found)


def _cell(row, index):
    if index is None or index >= len(row):
        return ""
    return (row[index] or "").strip()


def _resolve_column(spec, header, fallback, has_header):
    """列指定を 0 始まりの添字にする。番号でも列名でも受ける。"""
    if spec is None or spec == "":
        return fallback
    if isinstance(spec, int):
        index = spec - 1
    elif re.fullmatch(r"\d+", str(spec).strip()):
        index = int(str(spec).strip()) - 1
    else:
        if not has_header:
            raise TableError("見出しが無いので列名では指定できません: %s" % spec)
        target = str(spec).strip().lower()
        matches = [i for i, name in enumerate(header)
                   if (name or "").strip().lower() == target]
        if not matches:
            raise TableError("列が見つかりません: %s（列: %s）"
                             % (spec, ", ".join(header)))
        return matches[0]
    if index < 0:
        raise TableError("列番号は 1 以上で指定してください: %s" % spec)
    return index
