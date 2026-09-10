# -*- coding: utf-8 -*-
"""SIP/RTP テスト通話ジェネレータの GUI。

tkinter だけで作ってある。Python に標準で付いてくるので追加の
インストールが要らず、PyInstaller で 1 つの実行ファイルにまとめても
そのまま動く。配布先に Python を入れずに済ませたい、というのが
GUI をこの形にしている理由。

画面の構成は CLI の考え方と対応させてある。

  通話原稿  … 台本ファイル (gen_call.py の位置引数) と同じ内容
  接続設定  … 設定 JSON の sip_server / client / from / to / call /
              hold / media / network / sip_headers
  話者      … 台本から拾った話者と、設定 JSON の speakers による上書き
  取り込み  … 設定 JSON の service

［生成］を押すと、CLI と同じ builder.CallBuilder を呼ぶ。
生成処理は別スレッドで動かし、進捗はキュー経由で画面に流す
(合成に数秒かかるあいだ、画面が固まらないようにするため)。
"""

import io
import json
import os
import queue
import sys
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import (audio, builder, config, loader, script, service,
               speakers as speakers_mod, tabular, tts)

MONO = ("MS Gothic", 10)      # 日本語が出て等幅であること
TITLE = "SIP/RTP テスト通話ジェネレータ"

SAMPLE_SCRIPT = """\
# 1 行 1 発話。「話者：本文」と書くだけです。
# 話者名は自由で、事前の定義は要りません。
# OP のような名前は電話機側、CU のような名前はサーバ側に自動で振り分けます。

OP：お電話ありがとうございます。サポートセンターでございます。
CU：料金プランのことで確認したいことがありまして。

（0.6秒かぶせる）
OP：はい。

OP：お調べいたしますので、少々お待ちください。
（保留 6秒）
OP：大変お待たせいたしました。

（プッシュ音 CU：1234#）

CU：ありがとうございました。
（切断 OP）
"""

# 接続設定タブに並べる項目。(設定のキー, ラベル, 型, 補足)
CONNECTION_GROUPS = [
    ("SIP サーバ（PBX）", [
        (("sip_server", "ip"), "IP アドレス", "str", ""),
        (("sip_server", "port"), "SIP ポート", "int", ""),
        (("sip_server", "rtp_port"), "RTP ポート", "int", ""),
        (("sip_server", "domain"), "ドメイン", "str", "空欄ならサーバ IP を使う"),
    ]),
    ("クライアント電話機", [
        (("client", "ip"), "IP アドレス", "str", ""),
        (("client", "port"), "SIP ポート", "int", ""),
        (("client", "rtp_port"), "RTP ポート", "int", ""),
    ]),
    ("From / To（SIP ヘッダそのもの）", [
        (("from", "user"), "From ユーザ部", "str", "着信なら発信者番号"),
        (("from", "display"), "From 表示名", "str", "省略可"),
        (("to", "user"), "To ユーザ部", "str", "着信なら内線番号"),
        (("to", "display"), "To 表示名", "str", "省略可"),
    ]),
    ("通話の組み立て", [
        (("call", "ring_seconds"), "呼び出し秒数", "float", "180 から 200 OK まで"),
        (("call", "default_gap"), "発話間の間合い", "float", "秒"),
        (("call", "tail_seconds"), "終話前の余白", "float", "秒"),
        (("call", "start_time"), "開始時刻", "str", "ISO8601。空欄なら現在時刻"),
    ]),
]

CHOICES = {
    ("direction",): [("inbound", "着信（サーバから電話機へ INVITE）"),
                     ("outbound", "発信（電話機からサーバへ INVITE）")],
    ("codec",): [("PCMU", "PCMU（μ-law）"), ("PCMA", "PCMA（A-law）")],
    ("call", "hangup_by"): [("local", "電話機側から切る"),
                            ("remote", "サーバ側から切る")],
    ("hold", "mode"): [("sendonly", "sendonly（保留した側は送出継続）"),
                       ("inactive", "inactive（双方停止）")],
    ("media", "silence_mode"): [("continuous", "無音区間も送る"),
                                ("suppress", "無音区間は送らない")],
}


def _cache_dir():
    """合成音声のキャッシュ置き場。

    実行ファイルとして配布されると、置かれた場所に書き込めるとは限らない
    ので、ユーザのローカルアプリデータに固定する。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "sip-rtp-generator", "tts-cache")


def _default_output():
    documents = os.path.expanduser("~/Documents")
    folder = documents if os.path.isdir(documents) else os.path.expanduser("~")
    return os.path.join(folder, "testcall.pcap")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        self.geometry("1000x760")
        self.minsize(860, 620)

        self.vars = {}            # 設定キー -> tk 変数
        self.messages = queue.Queue()
        self.busy = False
        self.script_path = None
        self.config_path = None
        self.last_output = None

        self._build_menu()
        self._build_ui()
        self._apply_config(config.DEFAULTS)
        self._poll_messages()

    # ------------------------------------------------------------------
    # 画面の組み立て
    # ------------------------------------------------------------------
    def _build_menu(self):
        menu = tk.Menu(self)

        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label="原稿を開く…", command=self._open_script)
        file_menu.add_command(label="原稿を保存…", command=self._save_script)
        file_menu.add_separator()
        file_menu.add_command(label="設定を開く…", command=self._open_config)
        file_menu.add_command(label="設定を保存…", command=self._save_config)
        file_menu.add_separator()
        file_menu.add_command(label="設定を既定値に戻す", command=self._reset_config)
        file_menu.add_separator()
        file_menu.add_command(label="終了", command=self.destroy)
        menu.add_cascade(label="ファイル", menu=file_menu)

        tool_menu = tk.Menu(menu, tearoff=0)
        tool_menu.add_command(label="使える声を一覧表示", command=self._list_voices)
        tool_menu.add_command(label="音声キャッシュを削除", command=self._clear_cache)
        tool_menu.add_separator()
        tool_menu.add_command(label="出力フォルダを開く", command=self._open_folder)
        menu.add_cascade(label="ツール", menu=tool_menu)

        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="原稿の書き方", command=self._show_help)
        help_menu.add_command(label="バージョン情報", command=self._show_about)
        menu.add_cascade(label="ヘルプ", menu=help_menu)

        self.config(menu=menu)

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        notebook.add(self._tab_script(notebook), text="  通話原稿  ")
        notebook.add(self._tab_connection(notebook), text="  接続設定  ")
        notebook.add(self._tab_speakers(notebook), text="  話者  ")
        notebook.add(self._tab_service(notebook), text="  取り込み  ")

        self._build_action_bar()
        self._build_log()

    def _tab_script(self, parent):
        frame = ttk.Frame(parent, padding=8)

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="開く…", command=self._open_script).pack(side="left")
        ttk.Button(bar, text="保存…", command=self._save_script).pack(side="left", padx=4)
        ttk.Button(bar, text="書き方", command=self._show_help).pack(side="left")
        self.script_label = ttk.Label(bar, text="（未保存）", foreground="#666")
        self.script_label.pack(side="left", padx=10)

        self.script_text = ScrolledText(frame, font=MONO, undo=True, wrap="none")
        self.script_text.pack(fill="both", expand=True)
        self.script_text.insert("1.0", SAMPLE_SCRIPT)
        return frame

    def _tab_connection(self, parent):
        outer = ttk.Frame(parent, padding=8)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        top = ttk.LabelFrame(inner, text="呼の向き", padding=8)
        top.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        self._add_choice(top, 0, ("direction",), "向き")
        self._add_choice(top, 1, ("codec",), "コーデック")
        self._add_choice(top, 2, ("call", "hangup_by"), "切断する側")

        row = 1
        for title, fields in CONNECTION_GROUPS:
            group = ttk.LabelFrame(inner, text=title, padding=8)
            group.grid(row=row, column=0, sticky="ew", padx=4, pady=4)
            for i, (key, label, kind, hint) in enumerate(fields):
                self._add_entry(group, i, key, label, kind, hint)
            row += 1

        extra = ttk.LabelFrame(inner, text="保留とメディア", padding=8)
        extra.grid(row=row, column=0, sticky="ew", padx=4, pady=4)
        self._add_choice(extra, 0, ("hold", "mode"), "保留の方式")
        self._add_choice(extra, 1, ("media", "silence_mode"), "無音の扱い")
        self._add_entry(extra, 2, ("hold", "media"), "保留音 WAV", "str",
                        "空欄なら無音", browse="wav")
        self._add_entry(extra, 3, ("network", "jitter_ms"), "ジッタ (ms)", "float",
                        "送出時刻のゆらぎ。劣化テスト用")
        self._add_entry(extra, 4, ("network", "packet_loss"), "パケットロス率", "float",
                        "0.0〜1.0。劣化テスト用")

        headers = ttk.LabelFrame(inner, text="ACK に足すヘッダ", padding=8)
        headers.grid(row=row + 1, column=0, sticky="ew", padx=4, pady=4)
        ttk.Label(headers, wraplength=760, foreground="#444",
                  text="1 行に 1 つ「名前: 値」で書きます。"
                       "同じ名前があれば差し替わります。"
                       "{call_id} {branch} {cseq} {local_ip} {local_user} "
                       "{remote_ip} {remote_user} を値に差し込めます。"
                  ).pack(anchor="w", pady=(0, 4))
        self.ack_headers_text = ScrolledText(headers, font=MONO, height=4,
                                             wrap="none")
        self.ack_headers_text.pack(fill="x")
        ttk.Label(headers, foreground="#888",
                  text="From / To / Call-ID / CSeq / Via は呼の識別に使うため、"
                       "ここでは変えられません（From・To は上の欄で指定します）。"
                  ).pack(anchor="w", pady=(4, 0))
        return outer

    def _tab_speakers(self, parent):
        frame = ttk.Frame(parent, padding=8)

        ttk.Label(frame, wraplength=900, foreground="#444",
                  text="話者は台本から自動で拾います。ここで定義しなくても生成できます。"
                       "OP・オペレータ などは電話機側(local)、CU・お客様 などは"
                       "サーバ側(remote) に振り分けます。"
                       "変えたいときだけ、下の表で上書きしてください。"
                  ).pack(anchor="w", pady=(0, 6))

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="台本から読み込む",
                   command=self._load_speakers_from_script).pack(side="left")
        ttk.Button(bar, text="すべて消す（自動に戻す）",
                   command=self._clear_speakers).pack(side="left", padx=6)

        columns = ("name", "side", "voice", "rate", "pitch")
        headings = ("話者名", "side", "声", "速度", "ピッチ")
        self.speaker_tree = ttk.Treeview(frame, columns=columns, show="headings",
                                         height=7)
        for col, head in zip(columns, headings):
            self.speaker_tree.heading(col, text=head)
            self.speaker_tree.column(col, width=140 if col in ("name", "voice") else 90)
        self.speaker_tree.pack(fill="both", expand=True)
        self.speaker_tree.bind("<<TreeviewSelect>>", self._on_speaker_selected)

        editor = ttk.LabelFrame(frame, text="選択中の話者", padding=8)
        editor.pack(fill="x", pady=8)

        self.spk_name = tk.StringVar()
        self.spk_side = tk.StringVar(value="local")
        self.spk_voice = tk.StringVar()
        self.spk_rate = tk.StringVar(value="0")
        self.spk_pitch = tk.StringVar()

        ttk.Label(editor, text="話者名").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(editor, textvariable=self.spk_name, width=18).grid(
            row=0, column=1, sticky="w", padx=4)
        ttk.Label(editor, text="side").grid(row=0, column=2, sticky="w", padx=4)
        ttk.Combobox(editor, textvariable=self.spk_side, width=10, state="readonly",
                     values=["local", "remote"]).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(editor, text="声").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.voice_box = ttk.Combobox(editor, textvariable=self.spk_voice, width=28)
        self.voice_box.grid(row=1, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Button(editor, text="声を読み込む", command=self._load_voices).grid(
            row=1, column=3, sticky="w", padx=4)

        ttk.Label(editor, text="速度 (-10〜10)").grid(row=2, column=0, sticky="w",
                                                     padx=4, pady=3)
        ttk.Entry(editor, textvariable=self.spk_rate, width=8).grid(
            row=2, column=1, sticky="w", padx=4)
        ttk.Label(editor, text="ピッチ").grid(row=2, column=2, sticky="w", padx=4)
        ttk.Entry(editor, textvariable=self.spk_pitch, width=10).grid(
            row=2, column=3, sticky="w", padx=4)
        ttk.Label(editor, text='例: "+20%"', foreground="#666").grid(
            row=2, column=4, sticky="w")

        buttons = ttk.Frame(editor)
        buttons.grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="追加 / 更新", command=self._save_speaker).pack(side="left")
        ttk.Button(buttons, text="削除", command=self._delete_speaker).pack(side="left", padx=6)
        return frame

    def _tab_service(self, parent):
        frame = ttk.Frame(parent, padding=8)

        ttk.Label(frame, wraplength=900, foreground="#444",
                  text="pcap を生成したあと、音声認識サービスを停止し、"
                       "開始パラメータに pcap の絶対パスを付けて開始し直します。"
                       "{pcap} が生成した pcap のパスに置き換わります。"
                  ).pack(anchor="w", pady=(0, 8))

        group = ttk.LabelFrame(frame, text="サービス", padding=8)
        group.pack(fill="x")

        self.vars[("service", "enabled")] = tk.BooleanVar()
        ttk.Checkbutton(group, text="pcap を生成したあとサービスを入れ直して取り込ませる",
                        variable=self.vars[("service", "enabled")]).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=4, pady=4)

        self._add_entry(group, 1, ("service", "name"), "サービス名", "str", "")
        self._add_entry(group, 2, ("service", "start_args"), "開始パラメータ", "str",
                        "{pcap} は必須", width=60)
        self._add_entry(group, 3, ("service", "stop_timeout"), "待ち時間 (秒)", "int", "")

        actions = ttk.Frame(group)
        actions.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(actions, text="状態を確認", command=self._check_service).pack(side="left")
        ttk.Button(actions, text="発行するコマンドを表示",
                   command=self._preview_service).pack(side="left", padx=6)
        ttk.Button(actions, text="今の pcap を取り込ませる",
                   command=self._feed_now).pack(side="left")

        self.elevation_frame = ttk.LabelFrame(frame, text="権限", padding=8)
        self.elevation_frame.pack(fill="x", pady=8)
        self.elevation_label = ttk.Label(self.elevation_frame, text="確認中…")
        self.elevation_label.pack(side="left")
        self.elevate_button = ttk.Button(self.elevation_frame,
                                         text="管理者として起動し直す",
                                         command=self._elevate)
        self.elevate_button.pack(side="left", padx=10)
        self.after(200, self._refresh_elevation)
        return frame

    def _build_action_bar(self):
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")

        ttk.Label(bar, text="出力先").pack(side="left")
        self.output_var = tk.StringVar(value=_default_output())
        ttk.Entry(bar, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(bar, text="参照…", command=self._browse_output).pack(side="left")

        self.generate_button = ttk.Button(bar, text="pcap を生成",
                                          command=self._on_generate)
        self.generate_button.pack(side="left", padx=(12, 4))
        self.verify_button = ttk.Button(bar, text="検証", command=self._on_verify,
                                        state="disabled")
        self.verify_button.pack(side="left")
        ttk.Button(bar, text="フォルダを開く", command=self._open_folder).pack(
            side="left", padx=4)

    def _build_log(self):
        frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        frame.pack(fill="both", expand=True)

        header = ttk.Frame(frame)
        header.pack(fill="x")
        ttk.Label(header, text="ログ").pack(side="left")
        self.progress = ttk.Progressbar(header, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        ttk.Button(header, text="消去", command=self._clear_log).pack(side="right", padx=6)

        self.log_text = ScrolledText(frame, font=MONO, height=12, state="disabled",
                                     wrap="word", background="#1e1e1e",
                                     foreground="#e0e0e0")
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))
        for tag, color in (("error", "#ff8a80"), ("ok", "#a5d6a7"),
                           ("info", "#90caf9")):
            self.log_text.tag_configure(tag, foreground=color)

    # ------------------------------------------------------------------
    # 入力欄のヘルパ
    # ------------------------------------------------------------------
    def _add_entry(self, parent, row, key, label, kind, hint, width=26, browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=4, pady=3)
        var = tk.StringVar()
        self.vars[key] = var
        self.vars[("__kind__",) + key] = kind
        entry = ttk.Entry(parent, textvariable=var, width=width)
        entry.grid(row=row, column=1, sticky="w", padx=4)
        column = 2
        if browse == "wav":
            ttk.Button(parent, text="…", width=3,
                       command=lambda v=var: self._browse_into(v)).grid(
                row=row, column=column, sticky="w")
            column += 1
        if hint:
            ttk.Label(parent, text=hint, foreground="#666").grid(
                row=row, column=column, sticky="w", padx=6)

    def _add_choice(self, parent, row, key, label):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=4, pady=3)
        options = CHOICES[key]
        var = tk.StringVar()
        self.vars[key] = var
        self.vars[("__kind__",) + key] = "choice"
        self.vars[("__options__",) + key] = options
        box = ttk.Combobox(parent, textvariable=var, state="readonly", width=42,
                           values=[text for _, text in options])
        box.grid(row=row, column=1, columnspan=2, sticky="w", padx=4)

    # ------------------------------------------------------------------
    # 設定 <-> 画面
    # ------------------------------------------------------------------
    def _apply_config(self, cfg):
        for key, var in list(self.vars.items()):
            if key[0].startswith("__"):
                continue
            value = _dig(cfg, key)
            kind = self.vars.get(("__kind__",) + key)
            if kind == "choice":
                options = self.vars[("__options__",) + key]
                label = dict(options).get(value, options[0][1])
                var.set(label)
            elif isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("" if value is None else str(value))
        self._load_speakers(cfg["speakers"])
        self._set_ack_headers(_dig(cfg, ("sip_headers", "ACK")) or [])

    def _collect_config(self):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        for key, var in list(self.vars.items()):
            if key[0].startswith("__"):
                continue
            kind = self.vars.get(("__kind__",) + key)
            if kind == "choice":
                options = self.vars[("__options__",) + key]
                value = next((v for v, text in options if text == var.get()),
                             options[0][0])
            elif isinstance(var, tk.BooleanVar):
                value = bool(var.get())
            else:
                value = _parse(var.get(), kind, key)
                # 数値欄を空にしたら既定値のまま。None を入れると
                # 計算のときに落ちるので、上書きせずに残す
                if value is None and kind in ("int", "float"):
                    continue
            _bury(cfg, key, value)
        cfg["speakers"] = self._collect_speakers()
        cfg["sip_headers"] = dict(cfg.get("sip_headers") or {})
        cfg["sip_headers"]["ACK"] = self._collect_ack_headers()
        return config.load(None, cfg)

    def _collect_ack_headers(self):
        lines = self.ack_headers_text.get("1.0", "end-1c").splitlines()
        return [line.strip() for line in lines if line.strip()]

    def _set_ack_headers(self, entries):
        if isinstance(entries, dict):
            lines = ["%s: %s" % (k, v) for k, v in entries.items()]
        else:
            lines = list(entries)
        self.ack_headers_text.delete("1.0", "end")
        self.ack_headers_text.insert("1.0", "\n".join(lines))

    def _load_speakers(self, speakers):
        self.speaker_tree.delete(*self.speaker_tree.get_children())
        for name, spec in speakers.items():
            self.speaker_tree.insert("", "end", values=(
                name, spec.get("side", "local"), spec.get("voice", ""),
                spec.get("rate", 0), spec.get("pitch", "")))

    def _collect_speakers(self):
        speakers = {}
        for item in self.speaker_tree.get_children():
            name, side, voice, rate, pitch = self.speaker_tree.item(item, "values")
            spec = {"side": side}
            if voice:
                spec["voice"] = voice
            try:
                spec["rate"] = int(rate)
            except (TypeError, ValueError):
                spec["rate"] = 0
            if pitch:
                spec["pitch"] = pitch
            speakers[name] = spec
        # 空でもよい。台本から拾って側を推定するのが既定の流れで、
        # ここに並ぶのはその結果と、利用者が変えた上書きだけ
        return speakers

    def _load_speakers_from_script(self):
        """いまの台本を解析して、拾えた話者を表に並べる。"""
        try:
            parsed = self._parse_script()
        except (script.ScriptError, tabular.TableError) as exc:
            messagebox.showerror(TITLE, "台本の書式エラー\n\n%s" % exc)
            return
        resolved = speakers_mod.resolve(parsed.speakers, self._collect_speakers())
        ordered = {name: resolved[name] for name in parsed.speakers}
        self._load_speakers(ordered)
        self._log("台本から話者を読み込みました: %s"
                  % loader.describe_speakers(ordered, parsed.speakers), "info")

    def _clear_speakers(self):
        self.speaker_tree.delete(*self.speaker_tree.get_children())
        self._log("話者の上書きを消しました。台本から自動で判定します。", "info")

    def _parse_script(self):
        """画面の台本テキストを解析する。"""
        cfg = {"script": dict(config.DEFAULTS["script"])}
        base_dir = (os.path.dirname(self.script_path) if self.script_path
                    else os.path.dirname(os.path.abspath(self.output_var.get())))
        return loader.parse_text(self.script_text.get("1.0", "end"),
                                 base_dir=base_dir or ".", cfg=cfg)

    def _on_speaker_selected(self, _event=None):
        selection = self.speaker_tree.selection()
        if not selection:
            return
        name, side, voice, rate, pitch = self.speaker_tree.item(selection[0], "values")
        self.spk_name.set(name)
        self.spk_side.set(side)
        self.spk_voice.set(voice)
        self.spk_rate.set(rate)
        self.spk_pitch.set(pitch)

    def _save_speaker(self):
        name = self.spk_name.get().strip()
        if not name:
            messagebox.showwarning(TITLE, "話者名を入力してください。")
            return
        if ":" in name or "：" in name or name.startswith("@"):
            messagebox.showwarning(
                TITLE, "話者名に : や ： は使えません（@ で始めることもできません）。")
            return
        values = (name, self.spk_side.get(), self.spk_voice.get().strip(),
                  self.spk_rate.get().strip() or "0", self.spk_pitch.get().strip())
        for item in self.speaker_tree.get_children():
            if self.speaker_tree.item(item, "values")[0] == name:
                self.speaker_tree.item(item, values=values)
                return
        self.speaker_tree.insert("", "end", values=values)

    def _delete_speaker(self):
        for item in self.speaker_tree.selection():
            self.speaker_tree.delete(item)

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    def _on_generate(self):
        if self.busy:
            return
        try:
            cfg = self._collect_config()
        except (ValueError, KeyError) as exc:
            messagebox.showerror(TITLE, "設定を確認してください。\n\n%s" % exc)
            return

        out_path = self.output_var.get().strip()
        if not out_path:
            messagebox.showwarning(TITLE, "出力先を指定してください。")
            return
        if not out_path.lower().endswith(".pcap"):
            out_path += ".pcap"
            self.output_var.set(out_path)

        base_dir = (os.path.dirname(self.script_path) if self.script_path
                    else os.path.dirname(os.path.abspath(out_path)))
        try:
            parsed = loader.parse_text(self.script_text.get("1.0", "end"),
                                       base_dir=base_dir or ".", cfg=cfg)
            resolved = loader.resolve_speakers(cfg, parsed)
        except (script.ScriptError, tabular.TableError, loader.LoadError) as exc:
            messagebox.showerror(TITLE, "台本の書式エラー\n\n%s" % exc)
            return

        # 台本から拾った話者を画面にも反映して、どちら側になったか見せる
        self._load_speakers({name: resolved[name] for name in parsed.speakers})

        self._set_busy(True)
        self._clear_log()
        self._log("台本: %d 行" % len(parsed), "info")
        self._log("話者: %s" % loader.describe_speakers(resolved, parsed.speakers),
                  "info")
        threading.Thread(target=self._generate_worker,
                         args=(cfg, parsed, out_path, base_dir),
                         daemon=True).start()

    def _generate_worker(self, cfg, parsed, out_path, base_dir):
        try:
            call = builder.CallBuilder(cfg, parsed, base_dir=base_dir,
                                       cache_dir=_cache_dir(),
                                       log=lambda msg: self._post("log", msg))
            writer, transcript, _ = call.build()

            folder = os.path.dirname(os.path.abspath(out_path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            count = writer.write(out_path)

            transcript_path = os.path.splitext(out_path)[0] + ".json"
            with io.open(transcript_path, "w", encoding="utf-8") as f:
                json.dump(transcript, f, ensure_ascii=False, indent=2)

            self._post("log", "")
            for line in _summary(out_path, transcript_path, count, transcript):
                self._post("log", line)
            self._post("done", out_path)

            if cfg["service"]["enabled"]:
                self._post("log", "")
                svc = cfg["service"]
                args = service.restart_with_pcap(
                    svc["name"], svc["start_args"], out_path,
                    stop_timeout=svc["stop_timeout"],
                    start_timeout=svc["start_timeout"],
                    log=lambda msg: self._post("log", msg))
                self._post("ok", "認識サービスに取り込ませました: %s %s"
                           % (svc["name"], " ".join(args)))
        except (builder.BuildError, tts.TtsError, service.ServiceError,
                ValueError, OSError) as exc:
            self._post("error", str(exc))
        except Exception:
            self._post("error", "予期しないエラーが発生しました:\n%s"
                       % traceback.format_exc())
        finally:
            self._post("idle", None)

    def _on_verify(self):
        if self.busy or not self.last_output:
            return
        self._set_busy(True)
        self._log("")
        self._log("検証しています…", "info")
        threading.Thread(target=self._verify_worker, args=(self.last_output,),
                         daemon=True).start()

    def _verify_worker(self, path):
        import contextlib
        import verify_pcap
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = verify_pcap.verify(path, None, True)
            for line in buffer.getvalue().splitlines():
                self._post("log", line)
            self._post("ok" if code == 0 else "error",
                       "検証が完了しました。" if code == 0
                       else "検証で問題が見つかりました。")
        except Exception as exc:
            self._post("error", "検証に失敗しました: %s" % exc)
        finally:
            self._post("idle", None)

    # ------------------------------------------------------------------
    # サービス操作
    # ------------------------------------------------------------------
    def _service_settings(self):
        return (self.vars[("service", "name")].get().strip(),
                self.vars[("service", "start_args")].get().strip(),
                _parse(self.vars[("service", "stop_timeout")].get(), "int",
                       ("service", "stop_timeout")) or 30)

    def _check_service(self):
        name, _, _ = self._service_settings()
        if not name:
            messagebox.showwarning(TITLE, "サービス名を入力してください。")
            return
        status = service.status(name)
        if status is None:
            self._log("サービスが見つかりません: %s" % name, "error")
        else:
            self._log("%s: %s" % (name, status), "info")

    def _preview_service(self):
        name, template, _ = self._service_settings()
        target = self.last_output or self.output_var.get().strip()
        if not name or not template or not target:
            messagebox.showwarning(TITLE, "サービス名・開始パラメータ・出力先を入力してください。")
            return
        if "{pcap}" not in template:
            messagebox.showwarning(TITLE, "開始パラメータに {pcap} が入っていません。")
            return
        args = service.build_start_args(template, target)
        self._log("sc stop %s" % name)
        self._log("sc start %s %s" % (name, " ".join(args)))

    def _feed_now(self):
        if self.busy:
            return
        name, template, timeout = self._service_settings()
        if not self.last_output or not os.path.exists(self.last_output):
            messagebox.showwarning(TITLE, "先に pcap を生成してください。")
            return
        if "{pcap}" not in (template or ""):
            messagebox.showwarning(TITLE, "開始パラメータに {pcap} が入っていません。")
            return
        if not messagebox.askyesno(
                TITLE, "%s を停止して、次の pcap で開始し直します。\n\n%s\n\n続けますか？"
                       % (name, self.last_output)):
            return
        self._set_busy(True)
        threading.Thread(target=self._feed_worker,
                         args=(name, template, self.last_output, timeout),
                         daemon=True).start()

    def _feed_worker(self, name, template, path, timeout):
        try:
            args = service.restart_with_pcap(
                name, template, path, stop_timeout=timeout, start_timeout=timeout,
                log=lambda msg: self._post("log", msg))
            self._post("ok", "取り込ませました: %s %s" % (name, " ".join(args)))
        except service.ServiceError as exc:
            self._post("error", str(exc))
        finally:
            self._post("idle", None)

    def _refresh_elevation(self):
        try:
            elevated = service.is_elevated()
        except Exception:
            elevated = False
        if elevated:
            self.elevation_label.config(
                text="管理者として実行中です。サービスを操作できます。", foreground="#2e7d32")
            self.elevate_button.state(["disabled"])
        else:
            self.elevation_label.config(
                text="通常権限で実行中です。サービスの停止・開始には管理者権限が必要です。",
                foreground="#c62828")

    def _elevate(self):
        if not messagebox.askyesno(
                TITLE,
                "管理者として起動し直します。\n\n"
                "保存していない原稿と設定は失われます。\n"
                "先に［ファイル］→［原稿を保存］／［設定を保存］で保存してください。\n\n"
                "起動し直しますか？"):
            return
        try:
            import ctypes
            if getattr(sys, "frozen", False):
                target, params = sys.executable, ""
            else:
                target = sys.executable
                params = '"%s"' % os.path.abspath(sys.argv[0])
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", target, params, None, 1)
            if result <= 32:
                raise OSError("ShellExecuteW が %d を返しました" % result)
        except Exception as exc:
            messagebox.showerror(TITLE, "起動し直せませんでした:\n%s" % exc)
            return
        self.destroy()

    # ------------------------------------------------------------------
    # ファイル操作
    # ------------------------------------------------------------------
    def _open_script(self):
        path = filedialog.askopenfilename(
            title="通話原稿を開く",
            filetypes=[("テキスト", "*.txt"), ("すべて", "*.*")])
        if not path:
            return
        with io.open(path, encoding="utf-8-sig") as f:
            text = f.read()
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", text)
        self.script_path = path
        self.script_label.config(text=os.path.basename(path))

    def _save_script(self):
        path = filedialog.asksaveasfilename(
            title="通話原稿を保存", defaultextension=".txt",
            initialfile=os.path.basename(self.script_path or "script.txt"),
            filetypes=[("テキスト", "*.txt")])
        if not path:
            return
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(self.script_text.get("1.0", "end-1c"))
        self.script_path = path
        self.script_label.config(text=os.path.basename(path))
        self._log("原稿を保存しました: %s" % path, "ok")

    def _open_config(self):
        path = filedialog.askopenfilename(
            title="設定を開く", filetypes=[("JSON", "*.json"), ("すべて", "*.*")])
        if not path:
            return
        try:
            cfg = config.load(path)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(TITLE, "設定を読み込めませんでした:\n%s" % exc)
            return
        self._apply_config(cfg)
        self.config_path = path
        self._log("設定を読み込みました: %s" % path, "ok")

    def _save_config(self):
        try:
            cfg = self._collect_config()
        except (ValueError, KeyError) as exc:
            messagebox.showerror(TITLE, "設定を確認してください。\n\n%s" % exc)
            return
        path = filedialog.asksaveasfilename(
            title="設定を保存", defaultextension=".json",
            initialfile=os.path.basename(self.config_path or "config.json"),
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        with io.open(path, "w", encoding="utf-8") as f:
            # 内部でしか使わないキー（正規化済みヘッダなど）は落として保存する
            json.dump(config.public(cfg), f, ensure_ascii=False, indent=2)
        self.config_path = path
        self._log("設定を保存しました: %s" % path, "ok")

    def _reset_config(self):
        if messagebox.askyesno(TITLE, "設定を既定値に戻しますか？（原稿はそのままです）"):
            self._apply_config(config.DEFAULTS)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="出力先", defaultextension=".pcap",
            initialfile=os.path.basename(self.output_var.get() or "testcall.pcap"),
            filetypes=[("pcap", "*.pcap")])
        if path:
            self.output_var.set(path)

    def _browse_into(self, var):
        path = filedialog.askopenfilename(
            title="WAV を選ぶ", filetypes=[("WAV", "*.wav"), ("すべて", "*.*")])
        if path:
            var.set(path)

    def _open_folder(self):
        target = self.last_output or self.output_var.get()
        folder = os.path.dirname(os.path.abspath(target))
        if not os.path.isdir(folder):
            messagebox.showinfo(TITLE, "フォルダがまだありません: %s" % folder)
            return
        try:
            os.startfile(folder)
        except (AttributeError, OSError) as exc:
            messagebox.showerror(TITLE, "開けませんでした: %s" % exc)

    # ------------------------------------------------------------------
    # ツール
    # ------------------------------------------------------------------
    def _load_voices(self):
        try:
            voices = tts.list_voices()
        except tts.TtsError as exc:
            messagebox.showerror(TITLE, str(exc))
            return
        self.voice_box["values"] = [v["name"] for v in voices]
        self._log("使える声: %s" % ", ".join(v["name"] for v in voices), "info")

    def _list_voices(self):
        try:
            voices = tts.list_voices()
        except tts.TtsError as exc:
            messagebox.showerror(TITLE, str(exc))
            return
        if not voices:
            messagebox.showinfo(TITLE, "使える声が見つかりませんでした。")
            return
        body = "\n".join("%s  [%s / %s]" % (v["name"], v["culture"], v["gender"])
                         for v in voices)
        messagebox.showinfo(TITLE, "この PC で使える声:\n\n" + body +
                            "\n\n設定では部分一致で指定できます（例: Haruka）。")

    def _clear_cache(self):
        import shutil
        path = _cache_dir()
        if not os.path.isdir(path):
            messagebox.showinfo(TITLE, "キャッシュはありません。")
            return
        if messagebox.askyesno(TITLE, "音声キャッシュを削除しますか？\n\n%s" % path):
            shutil.rmtree(path, ignore_errors=True)
            self._log("音声キャッシュを削除しました。", "ok")

    def _show_help(self):
        window = tk.Toplevel(self)
        window.title("原稿の書き方")
        window.geometry("640x480")
        text = ScrolledText(window, font=MONO, wrap="word")
        text.pack(fill="both", expand=True)
        text.insert("1.0", HELP_TEXT)
        text.config(state="disabled")

    def _show_about(self):
        from . import __version__
        messagebox.showinfo(
            TITLE,
            "%s\nバージョン %s\n\n"
            "通話原稿から SIP/RTP のテスト通話 pcap を生成します。\n"
            "音声合成には Windows 標準の SAPI を使います。\n\n"
            "https://github.com/YoshikiIFUKU/sip-rtp-generator"
            % (TITLE, __version__))

    # ------------------------------------------------------------------
    # ログとスレッド間の受け渡し
    # ------------------------------------------------------------------
    def _post(self, kind, payload):
        self.messages.put((kind, payload))

    def _poll_messages(self):
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(payload)
            elif kind == "ok":
                self._log(payload, "ok")
            elif kind == "error":
                self._log(payload, "error")
            elif kind == "done":
                self.last_output = payload
                self.verify_button.state(["!disabled"])
            elif kind == "idle":
                self._set_busy(False)
        self.after(80, self._poll_messages)

    def _log(self, message, tag=None):
        self.log_text.config(state="normal")
        self.log_text.insert("end", (message or "") + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "!disabled"
        self.generate_button.state([state])
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
            if self.last_output:
                self.verify_button.state(["!disabled"])
            self._refresh_elevation()


HELP_TEXT = """台本の書き方
──────────────────────────────────────────

1 行 1 発話。「話者：本文」と書くだけです。

  OP：お電話ありがとうございます。
  CU：契約内容を確認したいのですが。

話者名は自由で、事前に定義する必要はありません。
名前から、どちら側の音声かを自動で判断します。

  OP / オペレータ / 担当 / 受付 / AGENT …… 電話機側（local）
  CU / お客様 / 顧客 / カスタマ / ユーザ …… サーバ側（remote）

当てはまらない名前は、出てきた順に電話機側 → サーバ側と割り当てます。
変えたいときは［話者］タブで上書きしてください。

区切りは全角「：」と半角「:」のどちらでも構いません。
行頭の # はコメント、空行は無視されます。


通話中の操作
──────────────────────────────────────────

行全体を丸括弧で囲むと指示になります。半角 ( ) でも書けます。

  （3秒あける）           間を空ける
  （0.6秒かぶせる）       次の発話を直前に食い込ませる（相づち・かぶり）
  （保留）                保留する。（保留解除）まで続く
  （保留 6秒）            6 秒だけ保留する
  （保留 OP）             掛ける側を指定する
  （保留解除）            保留を解除する
  （プッシュ音 CU：1234#）DTMF を送る。0-9 * # A-D が使えます
  （音声 OP：./ivr.wav）  既存の WAV をそのまま流す
  （切断 OP）             この側から BYE を送る

指示行には行末コメントも書けます。（3秒あける）  # 間を置く


例
──────────────────────────────────────────

  OP：お電話ありがとうございます。
  CU：契約内容を確認したいのですが。
  （0.6秒かぶせる）
  OP：はい。
  OP：お調べしますので少々お待ちください。
  （保留 5秒）
  OP：お待たせいたしました。
  （プッシュ音 CU：1234#）
  （切断 OP）


CSV / Excel から読む
──────────────────────────────────────────

「話者・開始時間・発言内容」の列を持つ CSV も、そのまま開けます。
列名から役割を自動で判定します（話者 / 発言内容(認識結果) / 開始時間 など）。

  話者,開始時間,発言内容
  OP,00:00.5,お電話ありがとうございます。
  CU,00:05.0,確認したいことがあります。

開始時間の列があれば、その時刻に発話を置きます。無ければ順に並びます。


覚えておくと便利なこと
──────────────────────────────────────────

* 発話と発話のあいだには［接続設定］の「発話間の間合い」が自動で入ります。
  （3秒あける）はそこに足し引きされます。

* 生成すると pcap と同じ場所に同名の .json が出ます。
  どの発話が何秒に入っているかが記録されているので、認識結果の
  答え合わせに使えます。

* 以前の @hold / @wait 形式の台本もそのまま読めます。
"""


def _summary(out_path, transcript_path, count, transcript):
    info = transcript["call"]
    stats = transcript["stats"]
    lines = [
        "pcap を書き出しました: %s" % out_path,
        "  Call-ID    : %s" % info["call_id"],
        "  向き       : %s  (%s → %s)" % (info["direction"], info["from"], info["to"]),
        "  コーデック : %s / %dms  RTP %s:%d ⇄ %s:%d"
        % (info["codec"], info["ptime"],
           transcript["streams"]["local"]["ip"], transcript["streams"]["local"]["port"],
           transcript["streams"]["remote"]["ip"], transcript["streams"]["remote"]["port"]),
        "  通話長     : %.1f 秒 (うち音声 %.1f 秒)"
        % (info["call_duration"], info["media_duration"]),
        "  パケット   : 合計 %d (SIP %d / RTP %d / DTMF %d)"
        % (count, len(transcript["sip"]), stats["rtp_packets"], stats["dtmf_packets"]),
    ]
    holds = [e for e in transcript["events"] if e["type"] == "hold"]
    if holds:
        lines.append("  保留       : %d 回 (%s)"
                     % (len(holds), ", ".join("%.1f-%.1f秒" % (h["start"], h["end"])
                                              for h in holds)))
    lines.append("  正解データ : %s" % transcript_path)
    return lines


def _dig(cfg, key):
    node = cfg
    for part in key:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _bury(cfg, key, value):
    node = cfg
    for part in key[:-1]:
        node = node.setdefault(part, {})
    node[key[-1]] = value


def _parse(text, kind, key):
    text = (text or "").strip()
    label = ".".join(key)
    if not text:
        return None
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            raise ValueError("%s には整数を入力してください: %s" % (label, text)) from None
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            raise ValueError("%s には数値を入力してください: %s" % (label, text)) from None
    return text


def main():
    app = App()
    app.mainloop()
    return 0
