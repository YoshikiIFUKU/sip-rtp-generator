#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成器の受け入れテスト。

  python tests/test_generate.py

音声合成を伴うテストは Windows の SAPI を呼ぶため、通話を組み立てる
テストではサイン波の WAV を代わりに流し込んでいる。合成が使えない
環境でも通るようにするため。
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

from sipgen import (audio, builder, config, loader, pcapw, script, service,
                    sipmsg, speakers, tabular)
from verify_pcap import looks_like_sip, parse_rtp, parse_sip, parse_udp, read_pcap


def make_tone_wav(path, seconds, freq=440, rate=8000, amplitude=16000):
    samples = [int(amplitude * math.sin(2 * math.pi * freq * i / rate))
               for i in range(int(seconds * rate))]
    audio.write_wav(path, samples, rate)
    return samples


def build_call(text, folder, cfg_overrides=None, seed=7, tone_seconds=1.0):
    """台本テキストから 1 通話ぶん組み立てる。発話は合成せず WAV に置き換える。"""
    tone = os.path.join(folder, "_tone.wav")
    if not os.path.exists(tone):
        make_tone_wav(tone, tone_seconds)

    cfg = config.load(None, cfg_overrides or {})
    parsed = loader.parse_text(text, base_dir=folder, cfg=cfg)
    loader.resolve_speakers(cfg, parsed)
    for event in parsed.events:
        if event["type"] == script.UTTERANCE:
            event.update({"type": script.WAV, "path": tone})
    call = builder.CallBuilder(cfg, parsed, base_dir=folder, seed=seed)
    return cfg, parsed, call.build()


# ----------------------------------------------------------------------
# 音声
# ----------------------------------------------------------------------
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
        import wave
        path = os.path.join(self.dir, "s.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(struct.pack("<%dh" % 1600, *([1000, 3000] * 800)))
        samples = audio.read_wav(path)
        self.assertEqual(len(samples), 800)
        self.assertEqual(samples[0], 2000)


# ----------------------------------------------------------------------
# 台本
# ----------------------------------------------------------------------
class TestScript(unittest.TestCase):
    def test_speaker_lines_need_no_definition(self):
        parsed = script.parse("OP：おはようございます。\nCU：はい。\n")
        self.assertEqual(parsed.speakers, ["OP", "CU"])
        self.assertEqual([e["type"] for e in parsed], ["utterance", "utterance"])

    def test_half_and_full_width_separators(self):
        parsed = script.parse("OP:半角\nCU：全角\n")
        self.assertEqual([e["text"] for e in parsed], ["半角", "全角"])

    def test_japanese_speaker_names(self):
        parsed = script.parse("オペレータ：いらっしゃいませ。\nお客様：これをください。\n")
        self.assertEqual(parsed.speakers, ["オペレータ", "お客様"])

    def test_custom_separator(self):
        parsed = script.parse("OP>こんにちは\nお客様|どうも\n", separators=">|")
        self.assertEqual([e["speaker"] for e in parsed], ["OP", "お客様"])

    def test_custom_pattern(self):
        parsed = script.parse("[OP] こんにちは\n", pattern=r"^\[(.+?)\]\s*(.*)$")
        self.assertEqual(parsed.events[0]["speaker"], "OP")
        self.assertEqual(parsed.events[0]["text"], "こんにちは")

    def test_colon_in_body_is_not_a_speaker(self):
        """話者名の長さ上限で、本文中のコロンを話者と読み違えないこと。"""
        long_line = "OP：受付時間は次のとおりです。平日は九時から十八時まで、" \
                    "土曜は九時から十七時まで、日曜と祝日はお休みです:ご注意ください"
        parsed = script.parse(long_line + "\n")
        self.assertEqual(parsed.events[0]["speaker"], "OP")
        self.assertIn(":ご注意ください", parsed.events[0]["text"])

    def test_comments_and_blank_lines(self):
        parsed = script.parse("# コメント\n\nOP：本文\n")
        self.assertEqual(len(parsed), 1)

    def test_directives(self):
        parsed = script.parse(
            "OP：あ\n"
            "（3秒あける）\n"
            "（0.6秒かぶせる）\n"
            "（保留 6秒）\n"
            "（保留解除）\n"
            "（プッシュ音 CU：1234#）\n"
            "（切断 OP）\n")
        kinds = [e["type"] for e in parsed]
        self.assertEqual(kinds, ["utterance", "wait", "wait", "hold", "unhold",
                                 "dtmf", "hangup"])
        self.assertEqual(parsed.events[1]["seconds"], 3.0)
        self.assertEqual(parsed.events[2]["seconds"], -0.6)
        self.assertEqual(parsed.events[3]["seconds"], 6.0)
        self.assertEqual(parsed.events[5]["digits"], "1234#")
        self.assertEqual(parsed.events[6]["speaker"], "OP")

    def test_directive_variants(self):
        for text, expected in (
                ("（3秒）", ("wait", 3.0)),
                ("（3）", ("wait", 3.0)),
                ("(2.5秒待つ)", ("wait", 2.5)),
                ("（かぶせて）", ("wait", -script.DEFAULT_OVERLAP)),
        ):
            parsed = script.parse("OP：あ\n%s\n" % text)
            event = parsed.events[1]
            self.assertEqual((event["type"], event["seconds"]), expected, text)

    def test_hold_with_speaker_and_seconds(self):
        parsed = script.parse("OP：あ\n（保留 IVR 5秒）\n")
        event = parsed.events[1]
        self.assertEqual((event["speaker"], event["seconds"]), ("IVR", 5.0))

    def test_numeric_speaker_name_survives(self):
        """CU2 のような数字入りの話者名を秒数と読み違えないこと。"""
        parsed = script.parse("CU2：あ\n（プッシュ音 CU2：99）\n")
        self.assertEqual(parsed.events[1]["speaker"], "CU2")
        self.assertEqual(parsed.events[1]["digits"], "99")

    def test_legacy_at_directives_still_work(self):
        parsed = script.parse(
            "agent: あ\n@wait -0.5\n@hold\n@unhold\n@dtmf customer: 12#\n@hangup agent\n")
        self.assertEqual([e["type"] for e in parsed],
                         ["utterance", "wait", "hold", "unhold", "dtmf", "hangup"])

    def test_trailing_comment_on_directive_only(self):
        parsed = script.parse("（3秒あける）  # 間を置く\nCU：#1番でお願いします。\n")
        self.assertEqual(parsed.events[0]["seconds"], 3.0)
        self.assertEqual(parsed.events[1]["text"], "#1番でお願いします。")

    def test_rejects_bad_input(self):
        for bad in ("区切りのない行", "（そんな指示はない）", "（保留 -3秒）",
                    "（プッシュ音 CU：9X）", "@nosuchthing"):
            with self.assertRaises(script.ScriptError, msg=bad):
                script.parse("OP：あ\n%s\n" % bad)

    def test_rejects_script_without_utterance(self):
        with self.assertRaises(script.ScriptError):
            script.parse("# コメントだけ\n")
        with self.assertRaises(script.ScriptError):
            script.parse("（3秒あける）\n")

    def test_known_speakers_can_be_enforced(self):
        with self.assertRaises(script.ScriptError):
            script.parse("XX：あ\n", speakers={"OP": {}, "CU": {}})


# ----------------------------------------------------------------------
# 話者の推定
# ----------------------------------------------------------------------
class TestSpeakers(unittest.TestCase):
    def test_operator_goes_to_the_phone_side(self):
        sides = speakers.guess_sides(["OP", "CU"])
        self.assertEqual(sides, {"OP": "local", "CU": "remote"})

    def test_japanese_names(self):
        sides = speakers.guess_sides(["オペレータ", "お客様"])
        self.assertEqual(sides["オペレータ"], "local")
        self.assertEqual(sides["お客様"], "remote")

    def test_order_does_not_matter_when_names_are_known(self):
        """IVR が先に出てきても、OP は電話機側に付くこと。"""
        sides = speakers.guess_sides(["IVR", "OP"])
        self.assertEqual(sides, {"IVR": "remote", "OP": "local"})

    def test_unknown_names_fill_in_order(self):
        sides = speakers.guess_sides(["甲", "乙", "丙"])
        self.assertEqual([sides["甲"], sides["乙"], sides["丙"]],
                         ["local", "remote", "remote"])

    def test_config_overrides_the_guess(self):
        resolved = speakers.resolve(["OP", "CU"], {"OP": {"side": "remote"}})
        self.assertEqual(resolved["OP"]["side"], "remote")
        self.assertEqual(resolved["CU"]["side"], "remote")

    def test_override_matching_ignores_case(self):
        resolved = speakers.resolve(["op"], {"OP": {"pitch": "+30%"}})
        self.assertEqual(resolved["op"]["pitch"], "+30%")

    def test_sides_get_distinguishable_voices(self):
        resolved = speakers.resolve(["OP", "CU"])
        self.assertNotEqual(resolved["OP"]["pitch"], resolved["CU"]["pitch"])

    def test_side_of_handles_case(self):
        resolved = speakers.resolve(["OP"])
        self.assertEqual(speakers.side_of(resolved, "op"), "local")
        self.assertIsNone(speakers.side_of(resolved, "だれか"))


# ----------------------------------------------------------------------
# CSV / TSV
# ----------------------------------------------------------------------
class TestTabular(unittest.TestCase):
    CSV = ("話者,開始時間,発言内容\n"
           "OP,00:00.5,お電話ありがとうございます。\n"
           "CU,00:05.0,確認したいことがあります。\n")

    def test_columns_are_guessed_from_header(self):
        parsed = tabular.parse(self.CSV)
        self.assertEqual(parsed.speakers, ["OP", "CU"])
        self.assertEqual(parsed.events[0]["start"], 0.5)
        self.assertEqual(parsed.events[1]["start"], 5.0)

    def test_recognition_style_headers(self):
        text = ("通話ID,開始時間(最新版数),音声のチャンネル種類,発言内容(認識結果)\n"
                "1,00:01.0,OP,はい\n1,00:03.0,CU,どうも\n")
        parsed = tabular.parse(text)
        self.assertEqual([e["speaker"] for e in parsed], ["OP", "CU"])
        self.assertEqual(parsed.events[1]["start"], 3.0)

    def test_time_formats(self):
        self.assertEqual(tabular.parse_time("00:03.0"), 3.0)
        self.assertEqual(tabular.parse_time("0:01:02.5"), 62.5)
        self.assertEqual(tabular.parse_time("3.2"), 3.2)
        self.assertIsNone(tabular.parse_time("あとで"))
        self.assertIsNone(tabular.parse_time(""))

    def test_tab_separated_without_header(self):
        text = "OP\t00:00.0\tはい\nCU\t00:02.0\tどうも\n"
        parsed = tabular.parse(text, has_header=False)
        self.assertEqual([e["speaker"] for e in parsed], ["OP", "CU"])
        self.assertEqual(parsed.events[1]["start"], 2.0)

    def test_columns_by_number(self):
        text = "x,OP,はい\ny,CU,どうも\n"
        parsed = tabular.parse(text, has_header=False, speaker_column=2,
                               text_column=3)
        self.assertEqual([e["speaker"] for e in parsed], ["OP", "CU"])

    def test_single_column_with_prefix(self):
        parsed = tabular.parse("本文\nOP：はい\nCU：どうも\n")
        self.assertEqual([e["speaker"] for e in parsed], ["OP", "CU"])

    def test_timings_can_be_ignored(self):
        parsed = tabular.parse(self.CSV, use_timings=False)
        self.assertNotIn("start", parsed.events[0])

    def test_describe_reports_roles(self):
        text = tabular.describe(self.CSV)
        self.assertIn("カンマ", text)
        self.assertIn("話者 として自動判定", text)

    def test_quoted_fields(self):
        text = '話者,発言内容\nOP,"はい、そうです、はい"\n'
        parsed = tabular.parse(text)
        self.assertEqual(parsed.events[0]["text"], "はい、そうです、はい")


class TestLoader(unittest.TestCase):
    def test_txt_is_read_as_script(self):
        parsed = loader.parse_text("OP：はい\nCU：どうも\n")
        self.assertEqual(parsed.speakers, ["OP", "CU"])

    def test_csv_text_is_detected(self):
        parsed = loader.parse_text("話者,発言内容\nOP,はい\nCU,どうも\n")
        self.assertEqual([e["speaker"] for e in parsed], ["OP", "CU"])

    def test_resolve_speakers_fills_config(self):
        cfg = config.load(None, {})
        parsed = loader.parse_text("OP：はい\nCU：どうも\n", cfg=cfg)
        resolved = loader.resolve_speakers(cfg, parsed)
        self.assertEqual(resolved["OP"]["side"], "local")
        self.assertEqual(cfg["speakers"], resolved)

    def test_describe_speakers(self):
        cfg = config.load(None, {})
        parsed = loader.parse_text("OP：はい\nCU：どうも\n", cfg=cfg)
        resolved = loader.resolve_speakers(cfg, parsed)
        text = loader.describe_speakers(resolved, parsed.speakers)
        self.assertEqual(text, "OP → 電話機側 / CU → サーバ側")


# ----------------------------------------------------------------------
# SIP / パケット
# ----------------------------------------------------------------------
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
        _, ctx = d.request("caller", "INVITE", in_dialog=False)
        resp, rctx = d.response(ctx, 200, "OK")
        self.assertIs(rctx["src"], ctx["dst"])
        self.assertIn(";tag=", parse_sip(resp.encode())["headers"]["to"][0])


def _dialog():
    a = sipmsg.Endpoint("10.0.0.1", 5060, b"\x00" * 6, "1001", domain="d")
    b = sipmsg.Endpoint("10.0.0.2", 5060, b"\x00" * 6, "2002", domain="d")
    return sipmsg.Dialog(a, b, "cid", "tag1", "tag2")


class TestCustomHeaders(unittest.TestCase):
    """ACK などに独自ヘッダを足せること。"""

    @staticmethod
    def _messages(cfg_overrides):
        folder = tempfile.mkdtemp()
        try:
            _, _, (writer, _, _) = build_call("OP：はい\nCU：どうも\n", folder,
                                              cfg_overrides, tone_seconds=0.3)
            path = os.path.join(folder, "h.pcap")
            writer.write(path)
            out = []
            for _, frame in read_pcap(path)[1]:
                info = parse_udp(frame)
                if info and looks_like_sip(info["payload"]):
                    out.append(info["payload"].decode("utf-8"))
            return out
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _ack(self, cfg_overrides):
        return next(m for m in self._messages(cfg_overrides) if m.startswith("ACK"))

    def test_header_is_added_to_ack(self):
        ack = self._ack({"sip_headers": {"ACK": ["X-Agent: 1001"]}})
        self.assertIn("\r\nX-Agent: 1001\r\n", ack)

    def test_placeholders_are_filled(self):
        ack = self._ack({"sip_headers": {"ACK": ["X-Call-Id: {call_id}",
                                                 "X-Who: {local_user}"]}})
        call_id = [l for l in ack.split("\r\n") if l.startswith("Call-ID:")][0]
        self.assertIn("X-Call-Id: %s" % call_id.split(": ", 1)[1], ack)
        self.assertIn("X-Who: 0312345678", ack)

    def test_existing_header_is_replaced_not_duplicated(self):
        ack = self._ack({"sip_headers": {"ACK": ["User-Agent: MyPhone/2.0"]}})
        agents = [l for l in ack.split("\r\n") if l.startswith("User-Agent:")]
        self.assertEqual(agents, ["User-Agent: MyPhone/2.0"])

    def test_null_removes_a_header(self):
        ack = self._ack({"sip_headers": {"ACK": {"User-Agent": None}}})
        self.assertNotIn("\r\nUser-Agent:", ack)

    def test_dict_form_is_accepted(self):
        ack = self._ack({"sip_headers": {"ACK": {"X-A": "1", "X-B": "2"}}})
        self.assertIn("\r\nX-A: 1\r\n", ack)
        self.assertIn("\r\nX-B: 2\r\n", ack)

    def test_headers_do_not_leak_to_other_messages(self):
        messages = self._messages({"sip_headers": {"ACK": ["X-Only: ack"]}})
        carrying = [m.split("\r\n")[0] for m in messages if "X-Only: ack" in m]
        self.assertEqual(len(carrying), 1)
        self.assertTrue(carrying[0].startswith("ACK"))

    def test_wildcard_applies_everywhere(self):
        messages = self._messages({"sip_headers": {"*": ["X-Run: 001"]}})
        self.assertTrue(all("X-Run: 001" in m for m in messages))

    def test_response_code_key(self):
        messages = self._messages({"sip_headers": {"200": ["X-Answered: yes"]}})
        for message in messages:
            first = message.split("\r\n")[0]
            self.assertEqual("X-Answered: yes" in message,
                             first.startswith("SIP/2.0 200"), first)

    def test_dialog_headers_are_protected(self):
        for name in ("From", "To", "Call-ID", "CSeq", "Via"):
            with self.assertRaises(ValueError, msg=name):
                config.load(None, {"sip_headers": {"ACK": ["%s: x" % name]}})

    def test_malformed_header_is_rejected(self):
        with self.assertRaises(ValueError):
            config.load(None, {"sip_headers": {"ACK": ["コロンなし"]}})

    def test_unknown_placeholder_is_left_alone(self):
        ack = self._ack({"sip_headers": {"ACK": ["X-Odd: {nosuchthing}"]}})
        self.assertIn("X-Odd: {nosuchthing}", ack)


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


# ----------------------------------------------------------------------
# 通話の組み立て
# ----------------------------------------------------------------------
class TestFullCall(unittest.TestCase):
    SCRIPT = ("OP：おはようございます。\n"
              "CU：はい。\n"
              "（保留 2秒）\n"
              "OP：お待たせしました。\n"
              "（プッシュ音 CU：12#）\n"
              "（切断 OP）\n")

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cfg, cls.parsed, (writer, cls.transcript, cls.plan) = build_call(
            cls.SCRIPT, cls.dir,
            {"call": {"start_time": "2026-01-01T09:00:00", "default_gap": 0.2}})
        cls.pcap = os.path.join(cls.dir, "call.pcap")
        writer.write(cls.pcap)

        _, packets = read_pcap(cls.pcap)
        cls.sip, cls.rtp = [], []
        for ts, frame in packets:
            info = parse_udp(frame)
            if looks_like_sip(info["payload"]):
                cls.sip.append((ts, info, parse_sip(info["payload"])))
            else:
                cls.rtp.append((ts, info, parse_rtp(info["payload"])))
        cls.t0 = packets[0][0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_speakers_were_inferred(self):
        self.assertEqual(self.cfg["speakers"]["OP"]["side"], "local")
        self.assertEqual(self.cfg["speakers"]["CU"]["side"], "remote")

    def test_sip_dialog_sequence(self):
        lines = [m[2]["start_line"].split()[0]
                 if not m[2]["start_line"].startswith("SIP")
                 else m[2]["start_line"][8:11] for m in self.sip]
        self.assertEqual(lines, ["INVITE", "100", "180", "200", "ACK",
                                 "INVITE", "200", "ACK",     # 保留
                                 "INVITE", "200", "ACK",     # 保留解除
                                 "BYE", "200"])

    def test_single_call_id(self):
        self.assertEqual(len({m[2]["headers"]["call-id"][0] for m in self.sip}), 1)

    def test_hold_uses_sendonly_then_sendrecv(self):
        directions = [d for m in self.sip
                      for d in ("sendonly", "recvonly", "inactive")
                      if "a=%s\r\n" % d in m[2]["body"]]
        self.assertEqual(directions, ["sendonly", "recvonly"])

    def test_all_checksums_valid(self):
        for _, info, _ in self.sip + self.rtp:
            self.assertTrue(info["ip_ok"] and info["udp_ok"])

    def test_two_rtp_streams_with_distinct_ssrc(self):
        self.assertEqual(len({p[2]["ssrc"] for p in self.rtp}), 2)

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
        self.assertAlmostEqual(hold["duration"], 2.0, places=2)
        media = self.transcript["call"]["media_start_offset"]
        window = (media + hold["start"] + 0.1, media + hold["end"] - 0.1)

        server_ip = self.cfg["sip_server"]["ip"]
        during = [p for ts, info, p in self.rtp
                  if info["src"] == server_ip and window[0] < ts - self.t0 < window[1]]
        self.assertEqual(during, [], "保留中にサーバ側から RTP が出ています")

        client_ip = self.cfg["client"]["ip"]
        holder = [p for ts, info, p in self.rtp
                  if info["src"] == client_ip and window[0] < ts - self.t0 < window[1]]
        self.assertTrue(holder, "a=sendonly なら保留側は送出を続けるはずです")

    def test_dtmf_events(self):
        dtmf = [p for _, _, p in self.rtp if p["pt"] == 101]
        self.assertEqual(len(dtmf), 3 * 8)      # 3 桁 × (5 パケット + 終了 3 回)
        self.assertEqual(sorted({d["payload"][0] for d in dtmf}), [1, 2, 11])
        self.assertEqual(len([d for d in dtmf if d["payload"][1] & 0x80]), 9)
        first = [d for d in dtmf if d["payload"][0] == 1]
        self.assertEqual(len({d["ts"] for d in first}), 1)

    def test_dtmf_is_on_the_customer_side(self):
        server_ip = self.cfg["sip_server"]["ip"]
        dtmf = [(info, p) for _, info, p in self.rtp if p["pt"] == 101]
        self.assertTrue(dtmf)
        self.assertTrue(all(info["src"] == server_ip for info, _ in dtmf))

    def test_transcript_timings_match_audio(self):
        streams = {}
        for _, info, pkt in self.rtp:
            if pkt["pt"] == 0:
                streams.setdefault(info["src"], []).append(pkt)
        ip_of = {"local": self.cfg["client"]["ip"],
                 "remote": self.cfg["sip_server"]["ip"]}

        for utt in self.transcript["utterances"]:
            packets = streams[ip_of[utt["side"]]]
            base = packets[0]["ts"]
            target = base + int(((utt["start"] + utt["end"]) / 2) * 8000)
            near = [p for p in packets
                    if min((p["ts"] - target) & 0xFFFFFFFF,
                           (target - p["ts"]) & 0xFFFFFFFF) < 160]
            self.assertTrue(near, "%s の音声が見つかりません" % utt["text"])
            samples = [audio._ULAW_DECODE[b] for b in near[0]["payload"]]
            self.assertGreater(max(abs(s) for s in samples), 1000)

    def test_transcript_is_valid_json(self):
        again = json.loads(json.dumps(self.transcript, ensure_ascii=False))
        self.assertEqual(again["call"]["direction"], "inbound")
        self.assertIn("streams", again)


class TestAbsoluteTimings(unittest.TestCase):
    """CSV の開始時間どおりに発話が置かれること。"""

    def test_start_times_are_honoured(self):
        folder = tempfile.mkdtemp()
        try:
            csv_text = ("話者,開始時間,発言内容\n"
                        "OP,00:02.0,はい\n"
                        "CU,00:10.0,どうも\n")
            _, _, (_, transcript, _) = build_call(csv_text, folder)
            starts = [u["start"] for u in transcript["utterances"]]
            self.assertEqual(starts, [2.0, 10.0])
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_without_timings_they_follow_each_other(self):
        folder = tempfile.mkdtemp()
        try:
            _, _, (_, transcript, _) = build_call(
                "OP：はい\nCU：どうも\n", folder,
                {"call": {"default_gap": 0.5}}, tone_seconds=1.0)
            starts = [u["start"] for u in transcript["utterances"]]
            self.assertEqual(starts[0], 0.0)
            self.assertAlmostEqual(starts[1], 1.5, places=2)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class TestDirection(unittest.TestCase):
    def _first_invite(self, direction):
        folder = tempfile.mkdtemp()
        try:
            _, _, (_, transcript, _) = build_call(
                "OP：はい\n", folder, {"direction": direction}, tone_seconds=0.5)
            return transcript["sip"][0]
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_inbound_invite_comes_from_server(self):
        first = self._first_invite("inbound")
        self.assertTrue(first["message"].startswith("INVITE"))
        self.assertTrue(first["from"].startswith(config.DEFAULTS["sip_server"]["ip"]))

    def test_outbound_invite_comes_from_phone(self):
        first = self._first_invite("outbound")
        self.assertTrue(first["message"].startswith("INVITE"))
        self.assertTrue(first["from"].startswith(config.DEFAULTS["client"]["ip"]))


# ----------------------------------------------------------------------
# 設定・サービス
# ----------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def test_rejects_unknown_codec(self):
        with self.assertRaises(ValueError):
            config.load(None, {"codec": "OPUS"})

    def test_rejects_unknown_side(self):
        with self.assertRaises(ValueError):
            config.load(None, {"speakers": {"OP": {"side": "middle"}}})

    def test_speakers_may_be_empty(self):
        self.assertEqual(config.load(None, {})["speakers"], {})

    def test_domain_defaults_to_server_ip(self):
        cfg = config.load(None, {"sip_server": {"ip": "10.9.8.7", "domain": None}})
        self.assertEqual(cfg["sip_server"]["domain"], "10.9.8.7")

    def test_overrides_are_deep_merged(self):
        cfg = config.load(None, {"client": {"ip": "10.0.0.9"}})
        self.assertEqual(cfg["client"]["ip"], "10.0.0.9")
        self.assertEqual(cfg["client"]["rtp_port"],
                         config.DEFAULTS["client"]["rtp_port"])

    def test_blank_required_field_is_rejected(self):
        with self.assertRaises(ValueError):
            config.load(None, {"sip_server": {"ip": ""}})


class TestServiceArgs(unittest.TestCase):
    def test_pcap_becomes_absolute_path(self):
        args = service.build_start_args(
            "--callid-generate --packet-sync {pcap}", "out/x.pcap")
        self.assertEqual(args[:2], ["--callid-generate", "--packet-sync"])
        self.assertTrue(os.path.isabs(args[2]))

    def test_path_with_spaces_stays_one_argument(self):
        args = service.build_start_args("--packet-sync {pcap}",
                                        "My Calls/test call.pcap")
        self.assertEqual(len(args), 2)
        self.assertIn("test call.pcap", args[1])

    def test_accepts_list_template(self):
        self.assertEqual(len(service.build_start_args(["--packet-sync", "{pcap}"],
                                                      "a.pcap")), 2)

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
        self.assertFalse(defaults["enabled"])


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


# ----------------------------------------------------------------------
# 同梱のサンプル
# ----------------------------------------------------------------------
class TestExamples(unittest.TestCase):
    def test_bundled_examples_parse(self):
        folder = os.path.join(ROOT, "examples")
        for name in os.listdir(folder):
            if not name.endswith((".txt", ".csv")):
                continue
            path = os.path.join(folder, name)
            with self.subTest(example=name):
                parsed = loader.parse_file(path)
                self.assertTrue(parsed.speakers, name)
                self.assertTrue(any(e["type"] in (script.UTTERANCE, script.WAV)
                                    for e in parsed), name)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class TestGui(unittest.TestCase):
    """画面 <-> 設定 の変換。ウィンドウは出さずに中身だけ確かめる。"""

    @classmethod
    def setUpClass(cls):
        try:
            from sipgen import gui
        except ImportError as exc:
            raise unittest.SkipTest("tkinter が使えません: %s" % exc)
        cls.gui = gui
        try:
            cls.app = gui.App()
        except Exception as exc:
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
        self.assertEqual(cfg["direction"], config.DEFAULTS["direction"])
        self.assertEqual(cfg["sip_server"]["ip"], config.DEFAULTS["sip_server"]["ip"])
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

    def test_blank_number_keeps_default(self):
        self.app.vars[("network", "jitter_ms")].set("")
        self.assertEqual(self.app._collect_config()["network"]["jitter_ms"],
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
        folder = tempfile.mkdtemp()
        try:
            cfg, parsed, (writer, transcript, _) = build_call(
                self.gui.SAMPLE_SCRIPT, folder, tone_seconds=0.4)
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
