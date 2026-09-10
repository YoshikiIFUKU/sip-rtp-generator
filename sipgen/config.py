"""設定 (JSON) の読み込みと既定値の補完。

利用者が書くのは「サーバ IP / 電話機 IP / From / To」の 4 つで足りる、
というのを最低ラインにして、それ以外はすべて既定値で埋める。
"""

import copy
import json
import os

DEFAULTS = {
    # 呼の向き。inbound = サーバから電話機へ着信 (コールセンターの受電)、
    # outbound = 電話機から発信
    "direction": "inbound",
    "sip_server": {
        "ip": "192.168.10.1",
        "port": 5060,
        "mac": "00:1a:2b:00:00:01",
        "domain": None,          # 未指定ならサーバ IP をドメインとして使う
        "rtp_port": 30000,
        "user_agent": "TestPBX/1.0",
    },
    "client": {
        "ip": "192.168.10.50",
        "port": 5060,
        "mac": "00:1a:2b:00:00:02",
        "rtp_port": 40000,
        "user_agent": "TestPhone/1.0",
    },
    # SIP の From / To ヘッダそのもの。inbound なら From が発信者番号、
    # To が内線番号になる
    "from": {"user": "0312345678", "display": None},
    "to": {"user": "1001", "display": None},
    "codec": "PCMU",
    "ptime": 20,                  # RTP 1 パケットあたりのミリ秒
    "call": {
        "start_time": None,       # pcap の先頭時刻 (ISO8601)。未指定なら現在時刻
        "ring_seconds": 2.0,      # 180 Ringing から 200 OK まで
        "answer_delay": 0.5,      # ACK から音声が始まるまで
        "tail_seconds": 1.0,      # 最後の発話から BYE まで
        "hangup_by": "local",     # local(電話機) / remote(サーバ側)
        "default_gap": 0.4,       # 発話と発話の間合い(秒)
    },
    "speakers": {
        # side は local(電話機側) / remote(サーバ側) のどちらの RTP に載せるか
        "agent":    {"side": "local",  "voice": "Haruka", "rate": 0, "pitch": "-5%"},
        "customer": {"side": "remote", "voice": "Haruka", "rate": -1, "pitch": "+18%"},
    },
    "hold": {
        "mode": "sendonly",       # sendonly(保留側は送出継続) / inactive(双方停止)
        "by": "local",            # 既定でどちら側が保留を掛けるか
        "media": None,            # 保留中に流す WAV。未指定なら無音
    },
    "dtmf": {
        "payload_type": 101,
        "digit_ms": 100,          # 1 桁の長さ
        "gap_ms": 60,             # 桁間の間隔
    },
    "media": {
        "silence_mode": "continuous",  # continuous(無音も送る) / suppress(無音区間は送らない)
        "normalize": True,             # 話者ごとの音量差をならす
    },
    "network": {
        "jitter_ms": 0.0,         # RTP 送出時刻に与えるゆらぎ(±)
        "packet_loss": 0.0,       # RTP のパケットロス率 (0.0-1.0)
    },
    # 生成した pcap を音声認識側に取り込ませるためのサービス操作。
    # {pcap} が生成した pcap の絶対パスに置き換わる
    "service": {
        "enabled": False,
        "name": "AmiVoiceRealTimeRecorder",
        "start_args": "--callid-generate --packet-sync {pcap}",
        "stop_timeout": 30,
        "start_timeout": 30,
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path=None, overrides=None):
    user = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    cfg = _merge(DEFAULTS, user)
    if overrides:
        cfg = _merge(cfg, {k: v for k, v in overrides.items() if v is not None})

    if not cfg["sip_server"].get("domain"):
        cfg["sip_server"]["domain"] = cfg["sip_server"]["ip"]
    if cfg["codec"] not in ("PCMU", "PCMA"):
        raise ValueError("codec は PCMU か PCMA を指定してください: %s" % cfg["codec"])
    if cfg["direction"] not in ("inbound", "outbound"):
        raise ValueError("direction は inbound か outbound を指定してください")
    for name, spk in cfg["speakers"].items():
        if spk.get("side") not in ("local", "remote"):
            raise ValueError("話者 %s の side は local か remote です" % name)
    if cfg["hold"].get("media"):
        path_ = cfg["hold"]["media"]
        if not os.path.exists(path_):
            raise ValueError("保留音の WAV が見つかりません: %s" % path_)
    svc = cfg["service"]
    if svc["enabled"]:
        if not svc.get("name"):
            raise ValueError("service.name にサービス名を指定してください")
        if "{pcap}" not in " ".join(_as_tokens(svc["start_args"])):
            raise ValueError(
                "service.start_args に {pcap} が入っていません。"
                "生成した pcap のパスを渡す位置を指定してください。")
    return cfg


def _as_tokens(value):
    return value.split() if isinstance(value, str) else list(value)


def write_example(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
