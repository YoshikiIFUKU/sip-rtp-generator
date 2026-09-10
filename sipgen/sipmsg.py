"""SIP メッセージの組み立てとダイアログ状態の管理。

呼制御を SIP から読み取る解析エンジンに食わせるのが目的なので、
タグ・ブランチ・CSeq・Contact といったダイアログ識別に使われる要素は
RFC 3261 の規則どおりに整合させる。ダイアログ内の CSeq は
UA ごとに独立した空間なので、両側ぶんを別々に数える。
"""

CRLF = "\r\n"

ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, UPDATE, PRACK, REFER, NOTIFY"

# 追加ヘッダをすべてのメッセージに適用するときのキー
ANY = "*"

# ここに挙げたヘッダは追加ヘッダで触らせない。ダイアログを識別する要素で、
# 書き換えると呼として成立しなくなるため。From / To は設定の from / to、
# Call-ID や CSeq や Via は生成側が一貫性を持たせて振っている。
PROTECTED_HEADERS = {
    "from": "設定の from",
    "to": "設定の to",
    "call-id": "自動採番",
    "cseq": "自動採番",
    "via": "自動生成",
}


def parse_custom_headers(spec):
    """設定に書かれた追加ヘッダを {キー: [(名前, 値), …]} に正規化する。

    値の書き方は 2 とおり受ける。

      "ACK": ["X-Call-Id: abc", "User-Agent: MyPhone/2.0"]
      "ACK": {"X-Call-Id": "abc", "User-Agent": null}

    値が None（JSON の null）だと、そのヘッダを取り除く。既にある名前を
    書けば上書きされるので、User-Agent や Contact も差し替えられる。
    """
    out = {}
    for key, value in (spec or {}).items():
        entries = []
        if isinstance(value, dict):
            items = value.items()
        else:
            items = []
            for line in (value or []):
                name, sep, body = str(line).partition(":")
                if not sep:
                    raise ValueError(
                        "ヘッダは『名前: 値』の形で書いてください: %s" % line)
                items.append((name, body))
        for name, body in items:
            name = str(name).strip()
            if not name:
                raise ValueError("ヘッダ名が空です: %s" % key)
            if name.lower() in PROTECTED_HEADERS:
                raise ValueError(
                    "%s ヘッダはここでは変えられません（%s で決まります）。"
                    "ダイアログの識別に使うため、生成側で一貫させています。"
                    % (name, PROTECTED_HEADERS[name.lower()]))
            entries.append((name, None if body is None else str(body).strip()))
        out[str(key).strip().upper()] = entries
    return out


def _find_header(headers, name):
    """開始行を除いて、同じ名前のヘッダの位置を返す。"""
    prefix = name.lower() + ":"
    for i in range(1, len(headers)):
        if headers[i].lower().startswith(prefix):
            return i
    return None


def apply_custom_headers(headers, entries, context=None):
    """追加ヘッダを反映する。同名は上書き、値が None なら削除。"""
    for name, value in entries or ():
        index = _find_header(headers, name)
        if value is None:
            if index is not None:
                headers.pop(index)
            continue
        try:
            rendered = value.format(**(context or {}))
        except (KeyError, IndexError, ValueError):
            # 差し込めない書式は、書かれたままの文字列として扱う
            rendered = value
        line = "%s: %s" % (name, rendered)
        if index is None:
            headers.append(line)
        else:
            headers[index] = line
    return headers


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

    def __init__(self, caller, callee, call_id, from_tag, to_tag, branch_seed=0,
                 custom_headers=None):
        self.caller = caller
        self.callee = callee
        self.call_id = call_id
        self.from_tag = from_tag
        self.to_tag = to_tag
        # メソッド名 / 応答コード / '*' をキーにした追加ヘッダ
        self.custom_headers = custom_headers or {}
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
        apply_custom_headers(headers, self._custom_for(method),
                             self._context(me, peer, cseq, branch, method))

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
        apply_custom_headers(
            headers, self._custom_for(str(code)),
            self._context(responder, ctx["src"], ctx["cseq"], ctx["branch"],
                          ctx["method"], code))

        resp_ctx = dict(ctx)
        resp_ctx.update({"src": responder, "dst": ctx["src"], "to_header": to_header})
        return _render(headers, body), resp_ctx

    def _custom_for(self, key):
        """'*' と、メソッド名（または応答コード）ぶんを重ねて返す。"""
        return (list(self.custom_headers.get(ANY, []))
                + list(self.custom_headers.get(key.upper(), [])))

    def _context(self, me, peer, cseq, branch, method, code=None):
        """ヘッダ値に差し込める項目。{call_id} のように書ける。"""
        return {
            "call_id": self.call_id, "from_tag": self.from_tag,
            "to_tag": self.to_tag, "branch": branch, "cseq": cseq,
            "method": method, "code": code if code is not None else "",
            "local_ip": me.ip, "local_port": me.port, "local_user": me.user,
            "remote_ip": peer.ip, "remote_port": peer.port,
            "remote_user": peer.user,
            "caller_user": self.caller.user, "callee_user": self.callee.user,
        }

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
