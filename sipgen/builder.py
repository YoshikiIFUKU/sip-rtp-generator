"""原稿と設定から 1 通話ぶんの pcap を組み立てる中核。

作り方は 3 段階に分かれている。

  1. 原稿を「時刻つきのメディア計画」に変換する (_plan_media)
     ここで各発話が何秒から何秒まで、どちらの向きの RTP に乗るかが決まる。
  2. SIP のシグナリングを時系列に並べる (_build_signaling)
     保留は re-INVITE として、メディア計画上の保留区間と同じ時刻に置く。
  3. メディア計画を 20ms 単位の RTP パケット列に展開する (_build_media)

この順序にしているのは、SIP と RTP が必ず 1 つのタイムラインを共有する
ようにするため。呼制御を SIP から、音声を RTP から読む解析系にとっては、
両者の時刻がずれていないことが最も重要になる。
"""

import datetime
import math
import os
import random
import time

from . import audio, pcapw, rtp, script, sipmsg, speakers, tts

SIDES = ("local", "remote")


class BuildError(Exception):
    pass


class CallBuilder:
    def __init__(self, cfg, events, base_dir=".", cache_dir=None, seed=None,
                 log=None):
        self.cfg = cfg
        self.events = events
        self.base_dir = base_dir
        self.cache_dir = cache_dir or os.path.join(base_dir, ".tts-cache")
        self.rnd = random.Random(seed if seed is not None else time.time())
        self.log = log or (lambda msg: None)

        self.codec = audio.CODECS[cfg["codec"]]
        self.ptime = cfg["ptime"]
        self.samples_per_packet = audio.seconds_to_samples(self.ptime / 1000.0)
        self.packet_seconds = self.ptime / 1000.0

        self._setup_endpoints()
        self.transcript = {"utterances": [], "events": [], "sip": []}
        self.rendered = {}

    # ------------------------------------------------------------------
    # 準備
    # ------------------------------------------------------------------
    def _setup_endpoints(self):
        cfg = self.cfg
        srv, cli = cfg["sip_server"], cfg["client"]

        def endpoint(phys, user_cfg):
            return sipmsg.Endpoint(
                ip=phys["ip"], port=phys["port"], mac=pcapw.parse_mac(phys["mac"]),
                user=str(user_cfg["user"]), display=user_cfg.get("display"),
                domain=srv["domain"], user_agent=phys["user_agent"])

        if cfg["direction"] == "inbound":
            # サーバ (PBX) が INVITE を出し、電話機が受ける
            self.caller = endpoint(srv, cfg["from"])
            self.callee = endpoint(cli, cfg["to"])
            self.side_of = {"remote": "caller", "local": "callee"}
        else:
            self.caller = endpoint(cli, cfg["from"])
            self.callee = endpoint(srv, cfg["to"])
            self.side_of = {"local": "caller", "remote": "callee"}
        self.role_of = {v: k for k, v in self.side_of.items()}

        self.rtp_port = {"local": cli["rtp_port"], "remote": srv["rtp_port"]}
        self.rtp_ip = {"local": cli["ip"], "remote": srv["ip"]}
        self.rtp_mac = {"local": pcapw.parse_mac(cli["mac"]),
                        "remote": pcapw.parse_mac(srv["mac"])}
        self.ip_bytes = {"local": pcapw.parse_ipv4(cli["ip"]),
                         "remote": pcapw.parse_ipv4(srv["ip"])}

        host = srv["ip"].replace(".", "-")
        self.dialog = sipmsg.Dialog(
            self.caller, self.callee,
            call_id="%08x-%s" % (self.rnd.getrandbits(32), host),
            from_tag="%08x" % self.rnd.getrandbits(32),
            to_tag="%08x" % self.rnd.getrandbits(32),
            custom_headers=cfg.get("_sip_headers"))
        self.sdp_session_id = {s: self.rnd.getrandbits(31) for s in SIDES}

    def _speaker_side(self, name):
        side = speakers.side_of(self.cfg["speakers"], name)
        if side is None:
            raise BuildError(
                "話者 %s の側が分かりません（設定にあるのは %s）"
                % (name, " / ".join(self.cfg["speakers"]) or "なし"))
        return side

    def _resolve_side(self, value, default):
        """保留・切断の対象を local / remote に読み替える。

        話者名でも、旧書式の local / remote でも受ける。
        """
        if not value:
            return default
        if value in SIDES:
            return value
        side = speakers.side_of(self.cfg["speakers"], value)
        if side is None:
            raise BuildError("話者名か local / remote を指定してください: %s" % value)
        return side

    # ------------------------------------------------------------------
    # 1. メディア計画
    # ------------------------------------------------------------------
    def _plan_media(self):
        gap = self.cfg["call"]["default_gap"]
        cursor = 0.0
        segments = {s: [] for s in SIDES}   # (開始秒, サンプル列)
        dtmfs = []                          # (開始秒, side, digits)
        holds = []                          # (開始秒, 終了秒, 保留を掛けた側)
        open_hold = None
        hangup_side = self.cfg["call"]["hangup_by"]

        for ev in self.events:
            kind = ev["type"]

            if kind == script.WAIT:
                cursor += ev["seconds"]

            elif kind in (script.UTTERANCE, script.WAV):
                side = self._speaker_side(ev["speaker"])
                samples, source = self._render_audio(ev)
                # CSV に開始時間の列があればその時刻に置く。無ければ順に並べる
                start = max(0.0, ev["start"] if ev.get("start") is not None
                            else cursor)
                segments[side].append((start, samples))
                duration = len(samples) / audio.SAMPLE_RATE
                self.transcript["utterances"].append({
                    "index": len(self.transcript["utterances"]),
                    "speaker": ev["speaker"], "side": side,
                    "start": round(start, 3), "end": round(start + duration, 3),
                    "duration": round(duration, 3),
                    "text": ev.get("text", os.path.basename(ev.get("path", ""))),
                    "source": source, "script_line": ev["line"],
                })
                cursor = start + duration + gap

            elif kind == script.DTMF:
                side = self._speaker_side(ev["speaker"])
                start = max(0.0, ev["start"] if ev.get("start") is not None
                            else cursor)
                digit_s = self.cfg["dtmf"]["digit_ms"] / 1000.0
                gap_s = self.cfg["dtmf"]["gap_ms"] / 1000.0
                duration = len(ev["digits"]) * (digit_s + gap_s)
                dtmfs.append((start, side, ev["digits"]))
                self.transcript["events"].append({
                    "type": "dtmf", "side": side, "speaker": ev["speaker"],
                    "digits": ev["digits"], "start": round(start, 3),
                    "end": round(start + duration, 3), "script_line": ev["line"],
                })
                cursor = start + duration + gap

            elif kind == script.HOLD:
                if open_hold is not None:
                    raise BuildError(
                        "%d 行目: 保留が解除されないまま、もう一度保留になっています"
                        % ev["line"])
                holder = self._resolve_side(ev.get("speaker"),
                                            self.cfg["hold"]["by"])
                start = max(0.0, cursor)
                if ev.get("seconds"):
                    # （保留 6秒）は、その場で解除まで済ませる
                    end = start + ev["seconds"]
                    holds.append((start, end, holder))
                    self.transcript["events"].append({
                        "type": "hold", "by": holder, "start": round(start, 3),
                        "end": round(end, 3), "duration": round(end - start, 3),
                        "mode": self.cfg["hold"]["mode"], "script_line": ev["line"],
                    })
                    cursor = end
                else:
                    open_hold = (start, holder, ev["line"])

            elif kind == script.UNHOLD:
                if open_hold is None:
                    raise BuildError(
                        "%d 行目: 保留していないのに保留解除になっています" % ev["line"])
                start, holder, line = open_hold
                end = max(start + self.packet_seconds, cursor)
                holds.append((start, end, holder))
                self.transcript["events"].append({
                    "type": "hold", "by": holder, "start": round(start, 3),
                    "end": round(end, 3), "duration": round(end - start, 3),
                    "mode": self.cfg["hold"]["mode"], "script_line": line,
                })
                open_hold = None

            elif kind == script.HANGUP:
                hangup_side = self._resolve_side(ev.get("speaker"), hangup_side)

        if open_hold is not None:
            # 解除されないまま原稿が終わったら、通話終了までを保留とみなす
            start, holder, line = open_hold
            holds.append((start, cursor, holder))
            self.transcript["events"].append({
                "type": "hold", "by": holder, "start": round(start, 3),
                "end": round(cursor, 3), "duration": round(cursor - start, 3),
                "mode": self.cfg["hold"]["mode"], "script_line": line,
                "note": "保留解除がないため通話終了まで保留",
            })

        duration = cursor + self.cfg["call"]["tail_seconds"]
        return {"segments": segments, "dtmfs": dtmfs, "holds": holds,
                "duration": max(duration, self.packet_seconds),
                "hangup_side": hangup_side}

    def _render_audio(self, ev):
        if ev["type"] == script.WAV:
            samples = audio.read_wav(ev["path"])
            source = "wav"
        else:
            spk = self.cfg["speakers"][ev["speaker"]]
            path, hit = tts.synthesize_cached(
                self.cache_dir, ev["text"],
                voice=spk.get("voice"), rate=spk.get("rate", 0),
                pitch=spk.get("pitch"), prosody_rate=spk.get("prosody_rate"),
                lang=spk.get("lang", "ja-JP"))
            self.log("  %s %-9s %s" % ("キャッシュ" if hit else "合成      ",
                                       ev["speaker"], ev["text"][:36]))
            samples = audio.read_wav(path)
            source = "tts"
        if self.cfg["media"]["normalize"]:
            samples = audio.normalize(samples)
        return samples, source

    # ------------------------------------------------------------------
    # 2. SIP シグナリング
    # ------------------------------------------------------------------
    def _sdp(self, side, direction, bump=True):
        role = self.side_of[side]
        version = self.dialog.next_sdp_version(role) if bump else 1
        return sipmsg.build_sdp(
            ip=self.rtp_ip[side], port=self.rtp_port[side],
            codec_name=self.codec["name"], codec_pt=self.codec["pt"],
            ptime=self.ptime, direction=direction,
            session_id=self.sdp_session_id[side], session_version=version,
            dtmf_pt=self.cfg["dtmf"]["payload_type"])

    def _emit_sip(self, when, text, ctx):
        src, dst = ctx["src"], ctx["dst"]
        frame = pcapw.build_udp_frame(
            src.mac, dst.mac,
            pcapw.parse_ipv4(src.ip), pcapw.parse_ipv4(dst.ip),
            src.port, dst.port, text.encode("utf-8"),
            ip_id=self.rnd.getrandbits(16))
        self.writer.add(self.start_epoch + when, frame)
        self.transcript["sip"].append({
            "time": round(when, 3),
            "abs_time": _iso(self.start_epoch + when),
            "from": "%s:%d" % (src.ip, src.port),
            "to": "%s:%d" % (dst.ip, dst.port),
            "message": text.split("\r\n", 1)[0],
        })

    def _build_signaling(self, plan):
        call = self.cfg["call"]

        t_invite = 0.0
        invite, ctx = self.dialog.request(
            "caller", "INVITE",
            body=self._sdp(self.role_of["caller"], "sendrecv"), in_dialog=False)
        self._emit_sip(t_invite, invite, ctx)

        trying, r = self.dialog.response(ctx, 100, "Trying", add_to_tag=False)
        self._emit_sip(t_invite + 0.02, trying, r)
        ringing, r = self.dialog.response(ctx, 180, "Ringing")
        self._emit_sip(t_invite + 0.30, ringing, r)

        t_answer = t_invite + 0.30 + call["ring_seconds"]
        ok, r = self.dialog.response(
            ctx, 200, "OK", body=self._sdp(self.role_of["callee"], "sendrecv"))
        self._emit_sip(t_answer, ok, r)

        t_ack = t_answer + 0.05
        ack, actx = self.dialog.ack(ctx)
        self._emit_sip(t_ack, ack, actx)

        media_start = t_ack + call["answer_delay"]

        # 保留 / 解除の re-INVITE。メディア側の保留区間と同じ時刻に置く
        mode = self.cfg["hold"]["mode"]
        hold_dir = "inactive" if mode == "inactive" else "sendonly"
        answer_dir = "inactive" if mode == "inactive" else "recvonly"
        for start, end, holder in plan["holds"]:
            self._reinvite(media_start + start, holder, hold_dir, answer_dir)
            self._reinvite(media_start + end, holder, "sendrecv", "sendrecv")

        t_bye = media_start + plan["duration"]
        bye, bctx = self.dialog.request(self.side_of[plan["hangup_side"]], "BYE")
        self._emit_sip(t_bye, bye, bctx)
        bye_ok, r = self.dialog.response(bctx, 200, "OK")
        self._emit_sip(t_bye + 0.03, bye_ok, r)

        return media_start, t_bye + 0.03

    def _reinvite(self, when, holder_side, offer_dir, answer_dir):
        text, ctx = self.dialog.request(
            self.side_of[holder_side], "INVITE",
            body=self._sdp(holder_side, offer_dir))
        self._emit_sip(when, text, ctx)
        peer_side = "remote" if holder_side == "local" else "local"
        ok, r = self.dialog.response(
            ctx, 200, "OK", body=self._sdp(peer_side, answer_dir))
        self._emit_sip(when + 0.03, ok, r)
        ack, actx = self.dialog.ack(ctx)
        self._emit_sip(when + 0.05, ack, actx)

    # ------------------------------------------------------------------
    # 3. メディア展開
    # ------------------------------------------------------------------
    def _mix(self, segments, total_samples):
        """発話を 1 本のバッファに重ね合わせる。

        @wait に負の値を書くと発話同士が重なるので、単純な代入ではなく
        加算にしてクリップする (実際の通話でも被りは起きるため)。
        """
        buf = [0] * total_samples
        for start, samples in segments:
            offset = audio.seconds_to_samples(start)
            for i, value in enumerate(samples):
                pos = offset + i
                if pos >= total_samples:
                    break
                mixed = buf[pos] + value
                if mixed > 32767:
                    mixed = 32767
                elif mixed < -32768:
                    mixed = -32768
                buf[pos] = mixed
        return buf

    def _gate_map(self, holds, n_slots):
        """保留により RTP を止める枠を side ごとに集合で持つ。

        a=sendonly の保留では、保留を掛けた側は送出を続け、掛けられた側
        からの音声が止まる。a=inactive では双方が止まる。
        """
        gated = {s: set() for s in SIDES}
        mode = self.cfg["hold"]["mode"]
        for start, end, holder in holds:
            peer = "remote" if holder == "local" else "local"
            first = max(0, int(start / self.packet_seconds))
            last = min(n_slots, int(math.ceil(end / self.packet_seconds)))
            for side in (SIDES if mode == "inactive" else (peer,)):
                gated[side].update(range(first, last))
        return gated

    def _dtmf_map(self, dtmfs, n_slots):
        """枠番号 -> その枠で送る DTMF パケットの内容。"""
        plans = {s: {} for s in SIDES}
        slots_per_digit = max(1, int(round(self.cfg["dtmf"]["digit_ms"] / self.ptime)))
        gap_slots = max(0, int(round(self.cfg["dtmf"]["gap_ms"] / self.ptime)))
        for start, side, digits in dtmfs:
            slot = int(round(start / self.packet_seconds))
            for digit in digits:
                event_start = slot
                placed = []
                for i in range(slots_per_digit):
                    if event_start + i >= n_slots:
                        break
                    plans[side][event_start + i] = {
                        "digit": digit, "event_start": event_start,
                        "packets": [(False, (i + 1) * self.samples_per_packet)],
                        "marker": i == 0,
                    }
                    placed.append(event_start + i)
                if placed:
                    # 終了パケットは 3 回送るのが RFC 2833 の推奨
                    total = slots_per_digit * self.samples_per_packet
                    plans[side][placed[-1]]["packets"].extend([(True, total)] * 3)
                slot += slots_per_digit + gap_slots
        return plans

    def _build_media(self, plan, media_start):
        n_slots = int(math.ceil(plan["duration"] / self.packet_seconds))
        total_samples = n_slots * self.samples_per_packet
        gated = self._gate_map(plan["holds"], n_slots)
        dtmf_plans = self._dtmf_map(plan["dtmfs"], n_slots)
        hold_media = self._hold_media(plan["holds"])

        jitter = self.cfg["network"]["jitter_ms"] / 1000.0
        loss = self.cfg["network"]["packet_loss"]
        suppress = self.cfg["media"]["silence_mode"] == "suppress"
        dtmf_pt = self.cfg["dtmf"]["payload_type"]
        stats = {"rtp_packets": 0, "dtmf_packets": 0, "dropped": 0, "suppressed": 0}
        self.transcript["streams"] = {}

        for side in SIDES:
            buf = self._mix(plan["segments"][side], total_samples)
            if hold_media.get(side):
                self._overlay_hold_media(buf, hold_media[side], total_samples)
            # --export-wav で聴いて確認できるよう、RTP に載せた音を残しておく
            self.rendered[side] = buf
            stream = rtp.RtpStream(
                ssrc=self.rnd.getrandbits(32),
                start_seq=self.rnd.getrandbits(15),
                base_timestamp=self.rnd.getrandbits(31),
                samples_per_packet=self.samples_per_packet)
            peer = "remote" if side == "local" else "local"
            sent_last = False

            for slot in range(n_slots):
                when = media_start + slot * self.packet_seconds
                if jitter:
                    when += self.rnd.uniform(-jitter, jitter)

                if slot in gated[side]:
                    sent_last = False
                    continue

                dtmf = dtmf_plans[side].get(slot)
                if dtmf:
                    ts = stream.timestamp_at(dtmf["event_start"])
                    for i, (end_flag, duration) in enumerate(dtmf["packets"]):
                        payload = rtp.build_dtmf_payload(dtmf["digit"], end_flag,
                                                         duration)
                        packet = stream.emit(dtmf_pt, payload, ts,
                                             marker=dtmf["marker"] and i == 0)
                        self._emit_rtp(side, peer, when + i * 0.0005, packet)
                        stats["dtmf_packets"] += 1
                    sent_last = True
                    continue

                window = buf[slot * self.samples_per_packet:
                             (slot + 1) * self.samples_per_packet]
                if suppress and not any(window):
                    stats["suppressed"] += 1
                    sent_last = False
                    continue
                if loss and self.rnd.random() < loss:
                    # 受信側から見て欠番になるよう、送らずにシーケンスだけ進める
                    stats["dropped"] += 1
                    stream.seq = (stream.seq + 1) & 0xFFFF
                    sent_last = False
                    continue

                payload = audio.encode(window, self.cfg["codec"])
                packet = stream.emit(self.codec["pt"], payload,
                                     stream.timestamp_at(slot), marker=not sent_last)
                self._emit_rtp(side, peer, when, packet)
                stats["rtp_packets"] += 1
                sent_last = True

            self.transcript["streams"][side] = {
                "ip": self.rtp_ip[side], "port": self.rtp_port[side],
                "ssrc": "0x%08x" % stream.ssrc,
                "payload_type": self.codec["pt"], "codec": self.codec["name"],
                "packets": stream.packet_count,
            }
        return stats

    def _hold_media(self, holds):
        """保留中に流す WAV を、保留を掛けた側のストリームに用意する。"""
        path = self.cfg["hold"].get("media")
        if not path or self.cfg["hold"]["mode"] == "inactive":
            return {}
        samples = audio.normalize(audio.read_wav(path))
        out = {}
        for start, end, holder in holds:
            out.setdefault(holder, []).append((start, end, samples))
        return out

    def _overlay_hold_media(self, buf, entries, total_samples):
        for start, end, samples in entries:
            if not samples:
                continue
            begin = audio.seconds_to_samples(start)
            stop = min(total_samples, audio.seconds_to_samples(end))
            for pos in range(max(0, begin), stop):
                buf[pos] = samples[(pos - begin) % len(samples)]

    def _emit_rtp(self, side, peer, when, packet):
        frame = pcapw.build_udp_frame(
            self.rtp_mac[side], self.rtp_mac[peer],
            self.ip_bytes[side], self.ip_bytes[peer],
            self.rtp_port[side], self.rtp_port[peer], packet,
            ip_id=self.rnd.getrandbits(16))
        self.writer.add(self.start_epoch + when, frame)

    # ------------------------------------------------------------------
    def build(self):
        self.writer = pcapw.PcapWriter()
        self.start_epoch = self._resolve_start_time()

        plan = self._plan_media()
        media_start, call_end = self._build_signaling(plan)
        stats = self._build_media(plan, media_start)

        for item in self.transcript["utterances"] + self.transcript["events"]:
            item["abs_start"] = _iso(self.start_epoch + media_start + item["start"])

        self.transcript["call"] = {
            "call_id": self.dialog.call_id,
            "direction": self.cfg["direction"],
            "from": self.caller.aor, "to": self.callee.aor,
            "codec": self.codec["name"], "ptime": self.ptime,
            "start_time": _iso(self.start_epoch),
            "media_start_offset": round(media_start, 3),
            "media_start": _iso(self.start_epoch + media_start),
            "media_duration": round(plan["duration"], 3),
            "call_duration": round(call_end, 3),
            "hangup_by": plan["hangup_side"],
        }
        self.transcript["stats"] = stats
        return self.writer, self.transcript, plan

    def _resolve_start_time(self):
        raw = self.cfg["call"].get("start_time")
        if not raw:
            return time.time()
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")
