"""RTP パケットの組み立て。

音声と RFC 2833 の DTMF は同じシーケンス空間を共有するが、
タイムスタンプの進み方が違う (DTMF はイベント開始時刻で固定)。
そこで RtpStream はシーケンス番号だけを持ち、タイムスタンプは
呼び出し側が「何番目の 20ms 枠か」から計算して渡す方式にする。
"""

import struct

RTP_VERSION = 2

DTMF_EVENTS = {str(d): d for d in range(10)}
DTMF_EVENTS.update({"*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15})


def build_packet(payload_type, seq, timestamp, ssrc, payload, marker=False):
    first = RTP_VERSION << 6
    second = ((1 if marker else 0) << 7) | (payload_type & 0x7F)
    header = struct.pack("!BBHII", first, second, seq & 0xFFFF,
                         timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + payload


def build_dtmf_payload(digit, end, duration_samples, volume=10):
    """RFC 2833 の telephone-event ペイロード (4 バイト)。

    duration は「イベント開始からの累積サンプル数」なので、
    同じイベント内で増えていく。
    """
    second = ((1 if end else 0) << 7) | (volume & 0x3F)
    return struct.pack("!BBH", DTMF_EVENTS[digit], second,
                       duration_samples & 0xFFFF)


class RtpStream:
    """片方向の RTP ストリーム 1 本ぶん。"""

    def __init__(self, ssrc, start_seq, base_timestamp, samples_per_packet):
        self.ssrc = ssrc
        self.seq = start_seq & 0xFFFF
        self.base_timestamp = base_timestamp & 0xFFFFFFFF
        self.samples_per_packet = samples_per_packet
        self.packet_count = 0

    def timestamp_at(self, slot):
        """slot 番目の 20ms 枠に対応する RTP タイムスタンプ。"""
        return (self.base_timestamp + slot * self.samples_per_packet) & 0xFFFFFFFF

    def emit(self, payload_type, payload, timestamp, marker=False):
        packet = build_packet(payload_type, self.seq, timestamp,
                              self.ssrc, payload, marker)
        self.seq = (self.seq + 1) & 0xFFFF
        self.packet_count += 1
        return packet
