"""Ethernet / IPv4 / UDP のフレーム組み立てと libpcap 形式の書き出し。

Wireshark や tcpreplay、および解析エンジンがそのまま食えるように、
チェックサムはすべて正しく計算する (IPv4 の UDP チェックサムは
省略可だが、0 のままだと壊れたキャプチャに見える環境があるため)。
"""

import struct

LINKTYPE_ETHERNET = 1
PCAP_MAGIC_USEC = 0xA1B2C3D4


def parse_mac(text):
    parts = text.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError("MAC アドレスの形式が不正です: %s" % text)
    return bytes(int(p, 16) for p in parts)


def parse_ipv4(text):
    parts = text.split(".")
    if len(parts) != 4:
        raise ValueError("IPv4 アドレスの形式が不正です: %s" % text)
    octets = [int(p) for p in parts]
    if any(o < 0 or o > 255 for o in octets):
        raise ValueError("IPv4 アドレスの範囲が不正です: %s" % text)
    return bytes(octets)


def _checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_udp_frame(src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port,
                    payload, ip_id=0, ttl=64):
    """1 つの UDP データグラムを Ethernet フレームにして返す。"""
    udp_len = 8 + len(payload)
    udp = struct.pack("!HHHH", src_port, dst_port, udp_len, 0) + payload

    # UDP チェックサムは IP 疑似ヘッダを含めて計算する
    pseudo = src_ip + dst_ip + struct.pack("!BBH", 0, 17, udp_len)
    csum = _checksum(pseudo + udp)
    if csum == 0:
        csum = 0xFFFF  # 0 は「チェックサム未計算」の意味になるため
    udp = udp[:6] + struct.pack("!H", csum) + udp[8:]

    total_len = 20 + udp_len
    ip_header = struct.pack(
        "!BBHHHBBH", 0x45, 0x00, total_len, ip_id & 0xFFFF, 0x4000, ttl, 17, 0
    ) + src_ip + dst_ip
    ip_header = ip_header[:10] + struct.pack("!H", _checksum(ip_header)) + ip_header[12:]

    return dst_mac + src_mac + b"\x08\x00" + ip_header + udp


class PcapWriter:
    """タイムスタンプ順に並べ替えてから 1 つの pcap に書き出す。

    SIP と 2 系統の RTP を別々に組み立てる都合上、パケットは時系列が
    ばらばらに出来上がる。キャプチャファイルとして正しく見えるよう、
    書き出し時にまとめてソートする。
    """

    def __init__(self, snaplen=65535):
        self.packets = []  # (timestamp, frame, 生成順)
        self.snaplen = snaplen

    def add(self, timestamp, frame):
        self.packets.append((timestamp, len(self.packets), frame))

    def write(self, path):
        self.packets.sort(key=lambda p: (p[0], p[1]))
        with open(path, "wb") as f:
            f.write(struct.pack(
                "<IHHiIII", PCAP_MAGIC_USEC, 2, 4, 0, 0, self.snaplen, LINKTYPE_ETHERNET
            ))
            for timestamp, _, frame in self.packets:
                sec = int(timestamp)
                usec = int(round((timestamp - sec) * 1_000_000))
                if usec >= 1_000_000:  # 丸め上がりの繰り上げ
                    sec += 1
                    usec -= 1_000_000
                f.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)))
                f.write(frame)
        return len(self.packets)
