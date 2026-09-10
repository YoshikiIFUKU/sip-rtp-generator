#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成器の受け入れテスト。

  python tests/test_generate.py

音声合成を伴うテストは Windows の SAPI を呼ぶため数秒かかる。
CI など合成が使えない環境では、その場でサイン波の WAV を作って
@wav 経由で流し込むテスト (test_full_call) だけでも通るようにしてある。
"""

import io
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sipgen import audio, builder, config, pcapw, script, service, sipmsg
from verify_pcap import looks_like_sip, parse_rtp, parse_sip, parse_udp, read_pcap


def make_tone_wav(path, seconds, freq=440, rate=8000, amplitude=16000):
    samples = [int(amplitude * math.sin(2 * math.pi * freq * i / rate))
               for i in range(int(seconds * rate))]
    audio.write_wav(path, samples, rate)
    return samples


class TestG711(unittest.TestCase):
    def test_round_trip_snr(self):
        sig = [int(20000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(4000)]
        for codec, table in (("PCMU", audio._ULAW_DECODE), ("PCMA", audio._ALAW_DECODE)):
            decoded = [table[b] for b in audio.encode(sig, codec)]
            noise = math.sqrt(sum((a - b) ** 2 for a, b in zip(sig, decoded)) / len(sig))
            signal = math.sqrt(sum(s * s for s in sig) / len(sig))
            snr = 20 * math.log10(signal / noise)
            self.assertGreater(snr, 30, "%s の往復 SNR が低すぎます: %.1f dB" % (codec, snr))

    def test_silence_encodes_to_standard_value(self):
        # 無音が規格どおりの符号になっていないと、無音区間を誤検出する装置がある
        self.assertEqual(audio.encode([0], "PCMU"), b"\xff")
        self.assertEqual(audio.encode([0], "PCMA"), b"\xd5")

    def test_clipping(self):
        for codec in ("PCMU", "PCMA"):
            self.assertEqual(len(audio.encode([32767, -32768], codec)), 2)


class TestAudioIO(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_resample_to_8k(self):
        path = os.path.join(self.dir, "a.wav")
        audio.write_wav(path, [0] * 16000, rate=16000)
        self.assertEqual(len(audio.read_wav(path)), 8000)

    def test_stereo_downmix(self):
        path = os.path.join(self.dir, "s.wav")
        import wave
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(struct.pack("<%dh" % 1600, *([1000, 3000] * 800)))
        samples = audio.read_wav(path)
        self.assertEqual(len(samples), 800)
        self.assertEqual(samples[0], 2000)


class TestScript(unittest.TestCase):
    def setUp(self):
        self.speakers = config.DEFAULTS["speakers"]

    def test_parses_all_directives(self):
        events = script.parse(
            "agent: あ\n@wait -0.5\n@hold\n@unhold\n@dtmf customer: 12#\n@hangup agent\n",
            self.speakers)
        self.assertEqual([e["type"] for e in events],
                         ["utterance", "wait", "hold", "unhold", "dtmf", "hangup"])
        self.assertEqual(events[1]["seconds"], -0.5)
        self.assertEqual(events[5]["side"], "local")

    def test_rejects_bad_input(self):
        for bad in ("unknown: hi", "@wait そのうち", "@dtmf agent: 12X",
                    "@nosuchthing", "agent:"):
            with self.assertRaises(script.ScriptError, msg=bad):
                script.parse(bad, self.speakers)

    def test_empty_script(self):
        with self.assertRaises(script.ScriptError):
            script.parse("# コメントだけ\n", self.speakers)

    def test_trailing_comments_are_stripped(self):
        events = script.parse(
            "agent: おはようございます。\n"
            "@wait -0.6            # 負の値で重ねる\n"
            "@hold                 # 保留する\n",
            self.speakers)
        self.assertEqual([e["type"] for e in events], ["utterance", "wait", "hold"])
        self.assertEqual(events[1]["seconds"], -0.6)

    def test_hash_without_leading_space_is_kept(self):
        """DTMF の # やセリフ中の # をコメント扱いしないこと。"""
        events = script.parse("@dtmf customer: 1234#\ncustomer: #1番でお願いします。\n",
                              self.speakers)
        self.assertEqual(events[0]["digits"], "1234#")
        self.assertEqual(events[1]["text"], "#1番でお願いします。")

    def test_dtmf_keeps_digits_when_commented(self):
        events = script.parse("@dtmf customer: 1234#  # 暗証番号\n", self.speakers)
        self.assertEqual(events[0]["digits"], "1234#")

    def test_gui_sample_script_parses(self):
        """GUI の初期表示がそのまま通ること（行末コメントを含む）。"""
        from sipgen import gui
        events = script.parse(gui.SAMPLE_SCRIPT, self.speakers)
        self.assertTrue(any(e["type"] == "dtmf" for e in events))
        self.assertTrue(any(e["type"] == "hold" for e in events))


class TestSip(unittest.TestCase):
    def test_body_uses_single_crlf(self):
        text, _ = _dialog().request("caller", "INVITE",
                                    body=sipmsg.build_sdp("10.0.0.1", 8000,
                                                          "PCMU", 0, 20),
                                    in_dialog=False)
        self.assertNotIn("\r\r\n", text)
        head, _, body = text.partition("\r\n\r\n")
        declared = [l for l in head.split("\r\n") if l.startswith("Content-Length")][0]
        self.assertEqual(int(declared.split(":")[1]), len(body.encode("utf-8")))

    def test_cseq_is_per_side(self):
        d = _dialog()
        _, a = d.request("caller", "INVITE", in_dialog=False)
        _, b = d.request("callee", "INVITE")
        _, c = d.request("caller", "BYE")
        self.assertEqual((a["cseq"], b["cseq"], c["cseq"]), (1, 1, 2))

    def test_response_swaps_direction_and_adds_tag(self):
        d = _dialog()
        text, ctx = d.request("caller", "INVITE", in_dialog=False)
        resp, rctx = d.response(ctx, 200, "OK")
        self.assertIs(rctx["src"], ctx["dst"])
        self.assertIn(";tag=", parse_sip(resp.encode())["headers"]["to"][0])


def _dialog():
    a = sipmsg.Endpoint("10.0.0.1", 5060, b"\x00" * 6, "1001", domain="d")
    b = sipmsg.Endpoint("10.0.0.2", 5060, b"\x00" * 6, "2002", domain="d")
    return sipmsg.Dialog(a, b, "cid", "tag1", "tag2")


class TestChecksums(unittest.TestCase):
    def test_frame_checksums_validate(self):
        frame = pcapw.build_udp_frame(
            pcapw.parse_mac("00:11:22:33:44:55"), pcapw.parse_mac("aa:bb:cc:dd:ee:ff"),
            pcapw.parse_ipv4("192.168.1.2"), pcapw.parse_ipv4("192.168.1.3"),
            5060, 5060, b"hello world", ip_id=1234)
        info = parse_udp(frame)
        self.assertTrue(info["ip_ok"])
        self.assertTrue(info["udp_ok"])
        self.assertEqual(info["payload"], b"hello world")


class TestFullCall(unittest.TestCase):
    """WAV だけで 1 通話を組み立て、pcap を読み直して構造を確かめる。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        tone_a = os.path.join(cls.dir, "a.wav")
        tone_b = os.path.join(cls.dir, "b.wav")
        make_tone_wav(tone_a, 1.0, 440)
        make_tone_wav(tone_b, 1.0, 880)

        text = "\n".join([
            "@wav agent: a.wav",
            "@wav customer: b.wav",
            "@hold",
            "@wait 2",
            "@unhold",
            "@wav agent: a.wav",
            "@dtmf customer: 12#",
            "@hangup agent",
        ])
        cfg = config.load(None, {"call": {"start_time": "2026-01-01T09:00:00",
                                          "default_gap": 0.2}})
        events = script.parse(text, cfg["speakers"], cls.dir)
        call = builder.CallBuilder(cfg, events, base_dir=cls.dir, seed=7)
        writer, cls.transcript, cls.plan = call.build()
        cls.pcap = os.path.join(cls.dir, "call.pcap")
        writer.write(cls.pcap)
        cls.cfg = cfg

        _, packets = read_pcap(cls.pcap)
        cls.sip, cls.rtp = [], []
        for ts, frame in packets:
            info = parse_udp(frame)
            cls.assertNotNone = info
            if looks_like_sip(info["payload"]):
                cls.sip.append((ts, info, parse_sip(info["payload"])))
            else:
                cls.rtp.append((ts, info, parse_rtp(info["payload"])))
        cls.t0 = packets[0][0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_sip_dialog_sequence(self):
        lines = [m[2]["start_line"].split()[0] if not m[2]["start_line"].startswith("SIP")
                 else m[2]["start_line"][8:11] for m in self.sip]
        self.assertEqual(lines, ["INVITE", "100", "180", "200", "ACK",
                                 "INVITE", "200", "ACK",     # 保留
                                 "INVITE", "200", "ACK",     # 保留解除
                                 "BYE", "200"])

    def test_single_call_id(self):
        ids = {m[2]["headers"]["call-id"][0] for m in self.sip}
        self.assertEqual(len(ids), 1)

    def test_hold_uses_sendonly_then_sendrecv(self):
        directions = [d for m in self.sip
                      for d in ("sendonly", "recvonly", "inactive")
                      if "a=%s\r\n" % d in m[2]["body"]]
        self.assertEqual(directions, ["sendonly", "recvonly"])

    def test_all_checksums_valid(self):
        for ts, info, _ in self.sip + self.rtp:
            self.assertTrue(info["ip_ok"] and info["udp_ok"])

    def test_two_rtp_streams_with_distinct_ssrc(self):
        ssrcs = {p[2]["ssrc"] for p in self.rtp}
        self.assertEqual(len(ssrcs), 2)

    def test_rtp_sequence_is_contiguous_per_stream(self):
        by_ssrc = {}
        for _, _, pkt in self.rtp:
            by_ssrc.setdefault(pkt["ssrc"], []).append(pkt["seq"])
        for ssrc, seqs in by_ssrc.items():
            expected = [(seqs[0] + i) & 0xFFFF for i in range(len(seqs))]
            self.assertEqual(seqs, expected, "SSRC 0x%08x の連番が飛んでいます" % ssrc)

    def test_audio_timestamp_advances_by_frame(self):
        by_ssrc = {}
        for _, _, pkt in self.rtp:
            if pkt["pt"] == 0:
                by_ssrc.setdefault(pkt["ssrc"], []).append(pkt["ts"])
        for tss in by_ssrc.values():
            deltas = {(b - a) & 0xFFFFFFFF for a, b in zip(tss, tss[1:])}
            self.assertTrue(all(d % 160 == 0 for d in deltas), deltas)

    def test_hold_stops_the_held_direction(self):
        hold = [e for e in self.transcript["events"] if e["type"] == "hold"][0]
        media = self.transcript["call"]["media_start_offset"]
        server_ip = self.cfg["sip_server"]["ip"]
        during = [p for ts, info, p in self.rtp
                  if info["src"] == server_ip
                  and media + hold["start"] + 0.1 < ts - self.t0 < media + hold["end"] - 0.1]
        self.assertEqual(during, [], "保留中にサーバ側から RTP が出ています")

        client_ip = self.cfg["client"]["ip"]
        holder = [p for ts, info, p in self.rtp
                  if info["src"] == client_ip
                  and media + hold["start"] + 0.1 < ts - self.t0 < media + hold["end"] - 0.1]
        self.assertTrue(holder, "a=sendonly なら保留側は送出を続けるはずです")

    def test_dtmf_events(self):
        dtmf = [p for _, _, p in self.rtp if p["pt"] == 101]
        # 3 桁 × (5 パケット + 終了 3 回)
        self.assertEqual(len(dtmf), 3 * 8)
        events = [d["payload"][0] for d in dtmf]
        self.assertEqual(sorted(set(events)), [1, 2, 11])  # 1, 2, #
        ends = [d for d in dtmf if d["payload"][1] & 0x80]
        self.assertEqual(len(ends), 9)
        # 同一イベント内でタイムスタンプが動かないこと
        first_event = [d for d in dtmf if d["payload"][0] == 1]
        self.assertEqual(len({d["ts"] for d in first_event}), 1)

    def test_transcript_timings_match_audio(self):
        """正解データの発話時刻に、実際にその側の音が入っているか。"""
        streams = {}
        for _, info, pkt in self.rtp:
            if pkt["pt"] == 0:
                streams.setdefault(info["src"], []).append(pkt)
        ip_of = {"local": self.cfg["client"]["ip"], "remote": self.cfg["sip_server"]["ip"]}

        for utt in self.transcript["utterances"]:
            packets = streams[ip_of[utt["side"]]]
            base = packets[0]["ts"]
            middle = (utt["start"] + utt["end"]) / 2
            target = base + int(middle * 8000)
            near = [p for p in packets if abs(((p["ts"] - target) & 0xFFFFFFFF)) < 160
                    or abs(((target - p["ts"]) & 0xFFFFFFFF)) < 160]
            self.assertTrue(near, "%s の音声が見つかりません" % utt["text"])
            samples = [audio._ULAW_DECODE[b] for b in near[0]["payload"]]
            energy = max(abs(s) for s in samples)
            self.assertGreater(energy, 1000,
                               "%.2f 秒時点の %s 側が無音です" % (middle, utt["side"]))

    def test_transcript_is_valid_json(self):
        text = json.dumps(self.transcript, ensure_ascii=False)
        again = json.loads(text)
        self.assertEqual(again["call"]["direction"], "inbound")
        self.assertIn("streams", again)


class TestDirection(unittest.TestCase):
    def _first_invite(self, direction):
        cfg = config.load(None, {"direction": direction})
        wav_dir = tempfile.mkdtemp()
        try:
            make_tone_wav(os.path.join(wav_dir, "t.wav"), 0.5)
            events = script.parse("@wav agent: t.wav\n", cfg["speakers"], wav_dir)
            writer, transcript, _ = builder.CallBuilder(
                cfg, events, base_dir=wav_dir, seed=1).build()
            return transcript["sip"][0]
        finally:
            shutil.rmtree(wav_dir, ignore_errors=True)

    def test_inbound_invite_comes_from_server(self):
        first = self._first_invite("inbound")
        self.assertTrue(first["message"].startswith("INVITE"))
        self.assertTrue(first["from"].startswith(config.DEFAULTS["sip_server"]["ip"]))

    def test_outbound_invite_comes_from_phone(self):
        first = self._first_invite("outbound")
        self.assertTrue(first["message"].startswith("INVITE"))
        self.assertTrue(first["from"].startswith(config.DEFAULTS["client"]["ip"]))


class TestConfig(unittest.TestCase):
    def test_rejects_unknown_codec(self):
        with self.assertRaises(ValueError):
            config.load(None, {"codec": "OPUS"})

    def test_rejects_unknown_side(self):
        with self.assertRaises(ValueError):
            config.load(None, {"speakers": {"agent": {"side": "middle"}}})

    def test_domain_defaults_to_server_ip(self):
        cfg = config.load(None, {"sip_server": {"ip": "10.9.8.7", "domain": None}})
        self.assertEqual(cfg["sip_server"]["domain"], "10.9.8.7")

    def test_overrides_are_deep_merged(self):
        cfg = config.load(None, {"client": {"ip": "10.0.0.9"}})
        self.assertEqual(cfg["client"]["ip"], "10.0.0.9")
        self.assertEqual(cfg["client"]["rtp_port"],
                         config.DEFAULTS["client"]["rtp_port"])


class TestServiceArgs(unittest.TestCase):
    """サービスの開始パラメータ組み立て。実際のサービスは操作しない。"""

    def test_pcap_becomes_absolute_path(self):
        args = service.build_start_args(
            "--callid-generate --packet-sync {pcap}", "out/x.pcap")
        self.assertEqual(args[:2], ["--callid-generate", "--packet-sync"])
        self.assertTrue(os.path.isabs(args[2]))
        self.assertTrue(args[2].endswith("x.pcap"))

    def test_path_with_spaces_stays_one_argument(self):
        args = service.build_start_args("--packet-sync {pcap}",
                                        "My Calls/test call.pcap")
        self.assertEqual(len(args), 2)
        self.assertIn("test call.pcap", args[1])

    def test_accepts_list_template(self):
        args = service.build_start_args(["--packet-sync", "{pcap}"], "a.pcap")
        self.assertEqual(len(args), 2)

    def test_placeholder_can_be_embedded(self):
        args = service.build_start_args("--input={pcap}", "a.pcap")
        self.assertEqual(len(args), 1)
        self.assertTrue(args[0].startswith("--input="))

    def test_missing_service_reports_none(self):
        self.assertIsNone(service.status("SipGenNoSuchService12345"))

    def test_default_matches_requested_values(self):
        defaults = config.DEFAULTS["service"]
        self.assertEqual(defaults["name"], "AmiVoiceRealTimeRecorder")
        self.assertEqual(defaults["start_args"],
                         "--callid-generate --packet-sync {pcap}")
        self.assertFalse(defaults["enabled"], "既定では無効であるべきです")


class TestServiceConfig(unittest.TestCase):
    def test_enabled_requires_pcap_placeholder(self):
        with self.assertRaises(ValueError):
            config.load(None, {"service": {"enabled": True,
                                           "start_args": "--callid-generate"}})

    def test_enabled_requires_name(self):
        with self.assertRaises(ValueError):
            config.load(None, {"service": {"enabled": True, "name": ""}})

    def test_disabled_config_is_not_validated(self):
        cfg = config.load(None, {"service": {"start_args": "なんでもよい"}})
        self.assertFalse(cfg["service"]["enabled"])

    def test_valid_config_passes(self):
        cfg = config.load(None, {"service": {"enabled": True,
                                             "name": "MyRecorder"}})
        self.assertEqual(cfg["service"]["name"], "MyRecorder")
        self.assertIn("{pcap}", cfg["service"]["start_args"])


class TestGui(unittest.TestCase):
    """画面 <-> 設定 の変換。ウィンドウは出さずに中身だけ確かめる。"""

    @classmethod
    def setUpClass(cls):
        try:
            from sipgen import gui
        except ImportError as exc:      # tkinter のない環境
            raise unittest.SkipTest("tkinter が使えません: %s" % exc)
        cls.gui = gui
        try:
            cls.app = gui.App()
        except Exception as exc:        # 画面のない環境
            raise unittest.SkipTest("画面を開けません: %s" % exc)
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "app", None) is not None:
            cls.app.destroy()

    def setUp(self):
        self.app._apply_config(config.DEFAULTS)

    def test_defaults_round_trip(self):
        cfg = self.app._collect_config()
        for key in ("direction", "codec"):
            self.assertEqual(cfg[key], config.DEFAULTS[key])
        self.assertEqual(cfg["sip_server"]["ip"], config.DEFAULTS["sip_server"]["ip"])
        self.assertEqual(cfg["speakers"].keys(), config.DEFAULTS["speakers"].keys())
        self.assertEqual(cfg["service"]["start_args"],
                         config.DEFAULTS["service"]["start_args"])

    def test_edits_survive_round_trip(self):
        self.app.vars[("sip_server", "ip")].set("10.1.2.3")
        self.app.vars[("call", "ring_seconds")].set("4.5")
        self.app.vars[("direction",)].set(
            dict(self.gui.CHOICES[("direction",)])["outbound"])

        cfg = self.app._collect_config()
        self.assertEqual(cfg["sip_server"]["ip"], "10.1.2.3")
        self.assertEqual(cfg["call"]["ring_seconds"], 4.5)
        self.assertEqual(cfg["direction"], "outbound")

        self.app._apply_config(cfg)
        self.assertEqual(self.app.vars[("sip_server", "ip")].get(), "10.1.2.3")
        self.assertEqual(self.app._collect_config()["direction"], "outbound")

    def test_blank_number_keeps_default(self):
        """数値欄を空にしても None にせず既定値を残すこと。"""
        self.app.vars[("network", "jitter_ms")].set("")
        cfg = self.app._collect_config()
        self.assertEqual(cfg["network"]["jitter_ms"],
                         config.DEFAULTS["network"]["jitter_ms"])

    def test_blank_required_field_is_rejected(self):
        self.app.vars[("sip_server", "ip")].set("")
        with self.assertRaises(ValueError):
            self.app._collect_config()

    def test_bad_number_is_rejected(self):
        self.app.vars[("call", "ring_seconds")].set("いつか")
        with self.assertRaises(ValueError):
            self.app._collect_config()

    def test_sample_script_builds_a_valid_call(self):
        cfg = self.app._collect_config()
        events = script.parse(self.gui.SAMPLE_SCRIPT, cfg["speakers"])
        # 音声合成を避けるため、発話はサイン波の WAV に差し替える
        folder = tempfile.mkdtemp()
        try:
            tone = os.path.join(folder, "t.wav")
            make_tone_wav(tone, 0.4)
            for event in events:
                if event["type"] == script.UTTERANCE:
                    event.update({"type": script.WAV, "path": tone})
            writer, transcript, _ = builder.CallBuilder(
                cfg, events, base_dir=folder, seed=5).build()
            path = os.path.join(folder, "gui.pcap")
            writer.write(path)
            _, packets = read_pcap(path)
            self.assertTrue(packets)
            self.assertTrue(any(e["type"] == "hold" for e in transcript["events"]))
            self.assertTrue(any(e["type"] == "dtmf" for e in transcript["events"]))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    unittest.main(verbosity=2)
