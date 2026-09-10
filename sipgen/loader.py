"""台本の読み込み口。

txt と CSV/TSV のどちらで書かれていても、同じ ParsedScript にして返す。
拡張子で決め打ちにせず、txt として読めなかったら表形式として読み直す。
TTSWavGenerator が「txt を読み込んで『話者：本文』として解釈できなかった
場合も、ウィザードで取り込むか確認する」としているのと同じ考え方。

話者は台本から拾って side を推定し、設定の speakers があればそれを
上書きとして重ねる。事前に話者を定義しなくても動くのが要点。
"""

import io
import os

from . import script, speakers, tabular


class LoadError(Exception):
    pass


def read_text(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return f.read()


def parse_text(text, base_dir=".", cfg=None, table_options=None, source="台本"):
    """台本テキストを ParsedScript にする。

    まず「話者：本文」の txt として読み、駄目なら表形式として読み直す。
    表形式のつもりの入力（拡張子が csv/tsv、あるいは 1 行目が既知の
    見出し）は最初から表として読む。
    """
    cfg = cfg or {}
    script_cfg = cfg.get("script", {})
    separators = script_cfg.get("separators") or script.DEFAULT_SEPARATORS
    pattern = script_cfg.get("pattern")
    options = dict(table_options or {})
    forced = options.pop("as_table", False)

    if forced or _looks_tabular(text, options):
        return _parse_table(text, base_dir, separators, options, source)

    try:
        return script.parse(text, base_dir=base_dir, separators=separators,
                            pattern=pattern)
    except script.ScriptError as script_error:
        # 区切り文字が入っていれば CSV として読み直してみる
        if not _has_delimiter(text):
            raise
        try:
            return _parse_table(text, base_dir, separators, options, source)
        except (tabular.TableError, script.ScriptError):
            raise script_error from None


def parse_file(path, cfg=None, table_options=None):
    text = read_text(path)
    base_dir = os.path.dirname(os.path.abspath(path)) or "."
    options = dict(table_options or {})
    if path.lower().endswith((".csv", ".tsv")):
        options.setdefault("as_table", True)
    if path.lower().endswith(".tsv"):
        options.setdefault("delimiter", "\t")
    return parse_text(text, base_dir=base_dir, cfg=cfg, table_options=options,
                      source=os.path.basename(path))


def _parse_table(text, base_dir, separators, options, source):
    try:
        return tabular.parse(text, base_dir=base_dir, separators=separators,
                             **options)
    except tabular.TableError as exc:
        raise script.ScriptError("%s: %s" % (source, exc)) from None


def _looks_tabular(text, options):
    if any(options.get(k) is not None for k in
           ("speaker_column", "text_column", "start_column", "delimiter")):
        return True
    first = next((l for l in text.splitlines() if l.strip()
                  and not l.strip().startswith("#")), "")
    if not first:
        return False
    delimiter = tabular.guess_delimiter(text)
    if delimiter not in first:
        return False
    return tabular.looks_like_header(first.split(delimiter))


def _has_delimiter(text):
    sample = "\n".join(text.splitlines()[:20])
    return any(d in sample for d in tabular.DELIMITERS)


def resolve_speakers(cfg, parsed):
    """台本に出てきた話者を設定に流し込む。

    cfg["speakers"] は上書きなので、書かれていない話者も
    名前からの推定で埋まる。cfg 自体を書き換えて返す。
    """
    cfg["speakers"] = speakers.resolve(parsed.speakers, cfg.get("speakers"))
    if not cfg["speakers"]:
        raise LoadError("台本に話者が 1 人もいません")
    return cfg["speakers"]


def describe_speakers(resolved, order=None):
    """『OP → 電話機側 / CU → サーバ側』のような説明を作る。"""
    names = order or list(resolved)
    labels = {"local": "電話機側", "remote": "サーバ側"}
    return " / ".join("%s → %s" % (name, labels[resolved[name]["side"]])
                      for name in names if name in resolved)
