"""SIP メッセージの組み立てとダイアログ状態の管理。

呼制御を SIP から読み取る解析エンジンに食わせるのが目的なので、
タグ・ブランチ・CSeq・Contact といったダイアログ識別に使われる要素は
RFC 3261 の規則どおりに整合させる。ダイアログ内の CSeq は
UA ごとに独立した空間なので、両側ぶんを別々に数える。
"""

CRLF = "\r\n"

ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, UPDATE, PRACK, REFER, NOTIFY"


class Endpoint:
    """ダイアログに参加する片側の UA。"""

    def __init__(self, ip, port, mac, user, display=None, domain=None,
                 user_agent="SIPTest/1.0"):
        self.ip = ip
        self.port = port
        self.mac = mac
        self.user = user
        self.display = display
        self.domain = domain or ip
        self.user_agent = user_agent

    @property
    def aor(self):
        """From / To に書く論理アドレス。"""
        uri = "sip:%s@%s" % (self.user, self.domain)
        if self.display:
            return '"%s" <%s>' % (self.display, uri)
        return "<%s>" % uri

    @property
    def contact(self):
        """実際に届く先。Contact と ACK/BYE の R-URI に使う。"""
        return "sip:%s@%s:%d" % (self.user, self.ip, self.port)


class Dialog:
    """1 通話ぶんの SIP ダイアログ。

    caller は最初の INVITE を出す側。inbound の呼では PBX が caller、
    outbound の呼では電話機が caller になる。
    """

    def __init__(self, caller, callee, call_id, from_tag, to_tag, branch_seed=0):
        self.caller = caller
        self.callee = callee
        self.call_id = call_id
        self.from_tag = from_tag
        self.to_tag = to_tag
        self._cseq = {"caller": 0, "callee": 0}
        self._branch_counter = branch_seed
        self._sdp_version = {"caller": 0, "callee": 0}

    # --- 内部ヘルパ ---------------------------------------------------
    def _next_branch(self):
        self._branch_counter += 1
        return "z9hG4bK%08x%04x" % (
            (hash(self.call_id) & 0xFFFFFFFF), self._branch_counter & 0xFFFF)

    def _roles(self, side):
        """side が出す要求における『自分 / 相手』と From / To のタグ。"""
        if side == "caller":
            return self.caller, self.callee, self.from_tag, self.to_tag
        return self.callee, self.caller, self.to_tag, self.from_tag

    def endpoint(self, side):
        return self.caller if side == "caller" else self.callee

    def next_sdp_version(self, side):
        self._sdp_version[side] += 1
        return self._sdp_version[side]

    # --- 要求 ---------------------------------------------------------
    def request(self, side, method, body=None, cseq=None, branch=None,
                extra_headers=None, in_dialog=True):
        """ダイアログ内の要求を組み立て、(テキスト, 送信情報) を返す。"""
        me, peer, my_tag, peer_tag = self._roles(side)
        if cseq is None:
            self._cseq[side] += 1
            cseq = self._cseq[side]
        branch = branch or self._next_branch()

        # 最初の INVITE だけは相手の Contact をまだ知らないので AoR 宛にする
        ruri = peer.contact if in_dialog else "sip:%s@%s" % (peer.user, peer.domain)

        to_header = peer.aor + (";tag=%s" % peer_tag if peer_tag and in_dialog else "")
        headers = [
            "%s %s SIP/2.0" % (method, ruri),
            "Via: SIP/2.0/UDP %s:%d;branch=%s;rport" % (me.ip, me.port, branch),
            "Max-Forwards: 70",
            "From: %s;tag=%s" % (me.aor, my_tag),
            "To: %s" % to_header,
            "Call-ID: %s" % self.call_id,
            "CSeq: %d %s" % (cseq, method),
            "Contact: <%s>" % me.contact,
            "User-Agent: %s" % me.user_agent,
        ]
        if method in ("INVITE", "OPTIONS"):
            headers.append("Allow: %s" % ALLOW)
            headers.append("Supported: timer, replaces")
        headers.extend(extra_headers or [])

        ctx = {
            "side": side, "method": method, "cseq": cseq, "branch": branch,
            "via_host": me.ip, "via_port": me.port,
            "from_header": "%s;tag=%s" % (me.aor, my_tag), "to_header": to_header,
            "src": me, "dst": peer,
        }
        return _render(headers, body), ctx

    # --- 応答 ---------------------------------------------------------
    def response(self, ctx, code, reason, body=None, add_to_tag=True,
                 extra_headers=None):
        """ctx で表される要求への応答。要求側と送受信の向きが逆になる。"""
        responder = ctx["dst"]
        to_header = ctx["to_header"]
        if add_to_tag and ";tag=" not in to_header:
            peer_tag = self.to_tag if ctx["side"] == "caller" else self.from_tag
            to_header = "%s;tag=%s" % (to_header, peer_tag)

        headers = [
            "SIP/2.0 %d %s" % (code, reason),
            "Via: SIP/2.0/UDP %s:%d;branch=%s;rport=%d;received=%s"
            % (ctx["via_host"], ctx["via_port"], ctx["branch"],
               ctx["via_port"], ctx["via_host"]),
            "From: %s" % ctx["from_header"],
            "To: %s" % to_header,
            "Call-ID: %s" % self.call_id,
            "CSeq: %d %s" % (ctx["cseq"], ctx["method"]),
        ]
        if code >= 180 and code < 300:
            headers.append("Contact: <%s>" % responder.contact)
        headers.append("User-Agent: %s" % responder.user_agent)
        if code == 200 and ctx["method"] == "INVITE":
            headers.append("Allow: %s" % ALLOW)
        headers.extend(extra_headers or [])

        resp_ctx = dict(ctx)
        resp_ctx.update({"src": responder, "dst": ctx["src"], "to_header": to_header})
        return _render(headers, body), resp_ctx

    def ack(self, ctx):
        """2xx への ACK。CSeq 番号は元の INVITE と同じで、ブランチだけ新しい。"""
        text, ack_ctx = self.request(
            ctx["side"], "ACK", cseq=ctx["cseq"])
        return text, ack_ctx


def _render(headers, body):
    # 本文の改行は必ず CRLF 1 つにそろえる。CRLF を含む文字列に対して
    # そのまま "\n" -> CRLF を掛けると "\r\r\n" になってしまうため、
    # いったん LF に落としてから変換する。
    payload = (body or "").replace(CRLF, "\n").replace("\r", "\n").replace("\n", CRLF)
    if payload:
        headers.append("Content-Type: application/sdp")
    headers.append("Content-Length: %d" % len(payload.encode("utf-8")))
    return CRLF.join(headers) + CRLF + CRLF + payload


def build_sdp(ip, port, codec_name, codec_pt, ptime, direction="sendrecv",
              session_id=None, session_version=1, dtmf_pt=101, session_name="SIP Call"):
    """音声 1 本ぶんの SDP。DTMF (telephone-event) も併せて広告する。

    direction には sendrecv / sendonly / recvonly / inactive が入る。
    保留はここを sendonly (または inactive) に変えることで表現する。
    """
    session_id = session_id or 1000000000
    formats = "%d" % codec_pt
    attrs = ["a=rtpmap:%d %s/8000" % (codec_pt, codec_name)]
    if dtmf_pt:
        formats += " %d" % dtmf_pt
        attrs.append("a=rtpmap:%d telephone-event/8000" % dtmf_pt)
        attrs.append("a=fmtp:%d 0-16" % dtmf_pt)
    attrs.append("a=ptime:%d" % ptime)
    attrs.append("a=%s" % direction)

    lines = [
        "v=0",
        "o=- %d %d IN IP4 %s" % (session_id, session_version, ip),
        "s=%s" % session_name,
        "c=IN IP4 %s" % ip,
        "t=0 0",
        "m=audio %d RTP/AVP %s" % (port, formats),
    ] + attrs
    return CRLF.join(lines) + CRLF
