"""台本に出てきた話者名から、その人がどちら側の RTP に乗るかを決める。

TTSWavGenerator が「あらかじめ決めた話者しか使えない、という作りにしない」
方針を採っているので、こちらも同じにする。台本に出てきた名前をそのまま
拾い、名前から側を推定し、設定で上書きできる、という順番にする。

側の対応は次のとおり。

  local  … クライアント電話機側。オペレーターが座っている側
  remote … SIP サーバ側。外線の相手 (お客様) 側

着信でも発信でも、電話機の前にいるのはオペレーターなので、
OP 系の名前は常に local に寄せてよい。
"""

# 名前からの推定に使う手がかり。TTSWavGenerator の R/L 推定と同じ語を使い、
# 向こうで OP=右 だったものを local、CU=左 だったものを remote に対応させる。
OPERATOR_HINTS = ("OP", "OPE", "OPERATOR", "オペレータ", "オペレーター",
                  "担当", "応対", "受付", "AGENT", "STAFF", "店員")
CUSTOMER_HINTS = ("CU", "CUST", "CUSTOMER", "お客様", "お客さま", "顧客",
                  "カスタマ", "カスタマー", "客", "ユーザ", "ユーザー")

LOCAL = "local"
REMOTE = "remote"

# 声が 1 種類しかない環境でも聞き分けられるよう、既定で少し差をつける
DEFAULT_LOCAL = {"voice": "Haruka", "rate": 0, "pitch": "+0%"}
DEFAULT_REMOTE = {"voice": "Haruka", "rate": -1, "pitch": "-12%"}


def normalize(name):
    """話者名の表記ゆれを吸収した照合用のキー。"""
    return (name or "").strip().upper()


def _matches(name, hints):
    key = normalize(name)
    if any(key == hint.upper() for hint in hints):
        return True
    return any(hint.upper() in key for hint in hints)


def guess_sides(names):
    """出現順の話者名から side の初期値を決める。

    名前で判断できるものを先に押さえ、残りを出現順に local → remote と
    埋める。3 人目以降は外線側 (remote) に置く。会議のように複数人が
    相手側にいる場合が自然だから。
    """
    sides = {}
    local_taken = remote_taken = False

    for name in names:
        if not local_taken and _matches(name, OPERATOR_HINTS):
            sides[name] = LOCAL
            local_taken = True
        elif not remote_taken and _matches(name, CUSTOMER_HINTS):
            sides[name] = REMOTE
            remote_taken = True

    for name in names:
        if name in sides:
            continue
        if not local_taken:
            sides[name] = LOCAL
            local_taken = True
        elif not remote_taken:
            sides[name] = REMOTE
            remote_taken = True
        else:
            sides[name] = REMOTE
    return sides


def resolve(names, overrides=None):
    """話者名の一覧から、生成に使う話者設定を組み立てる。

    overrides は設定ファイルの speakers。台本に出てこない話者が
    書かれていても無視せず残す (原稿を書き換える前に設定だけ
    用意しておく、という使い方ができるように)。
    """
    overrides = overrides or {}
    lookup = {normalize(k): v for k, v in overrides.items()}

    ordered = []
    for name in names:
        if name not in ordered:
            ordered.append(name)

    sides = guess_sides(ordered)
    speakers = {}
    for name in ordered:
        override = lookup.get(normalize(name), {})
        side = override.get("side") or sides[name]
        base = dict(DEFAULT_LOCAL if side == LOCAL else DEFAULT_REMOTE)
        base["side"] = side
        for key, value in override.items():
            if value is not None:
                base[key] = value
        speakers[name] = base

    # 台本に出てこない設定も残す。side が書かれていなければ remote 扱い
    for name, override in overrides.items():
        if name in speakers:
            continue
        side = override.get("side") or REMOTE
        base = dict(DEFAULT_LOCAL if side == LOCAL else DEFAULT_REMOTE)
        base["side"] = side
        base.update({k: v for k, v in override.items() if v is not None})
        speakers[name] = base
    return speakers


def side_of(speakers, name):
    """話者名から side を引く。大文字小文字とゆれを吸収する。"""
    if name in speakers:
        return speakers[name]["side"]
    key = normalize(name)
    for candidate, spec in speakers.items():
        if normalize(candidate) == key:
            return spec["side"]
    return None
