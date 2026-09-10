# SIP/RTP テスト通話ジェネレータ

通話の台本（テキスト）から、**SIP のシグナリングと RTP の音声が入った pcap** を作ります。
コールセンター向けの音声認識システムのように「呼制御を SIP から読み、音声を RTP から認識する」
仕組みに、実機や実回線なしでテスト通話を流し込むためのツールです。

```
台本 (.txt/.csv) ─┬─→  Windows の音声合成  ──→  8kHz G.711  ──→  RTP  ──┐
                  │                                                     ├─→ call.pcap
設定 (.json) ─────┴─→  SIP ダイアログ (INVITE / 保留 / BYE)  ──────────┘
                                                                        └─→ call.json（正解データ）
```

* **GUI と CLI の両方**があります。GUI は実行ファイル 1 つで配布でき、**配布先に Python は不要**です
* Python から動かす場合も **追加ライブラリのインストールは不要**（3.8 以降の標準ライブラリのみ）
* 音声合成は Windows 標準の SAPI（System.Speech）を使うので、**オフラインで完結**します
* 生成と同時に、発話内容と時刻を記録した **正解データ JSON** が出るので、認識結果の突き合わせにそのまま使えます

台本の書式は、既に使っている台本ツールにそろえてあります。
同じ台本ファイルを、WAV を作るときにも pcap を作るときにも使えます。

---

## 1. 台本の書き方

**1 行 1 発話。「話者：本文」と書くだけです。**

```text
# 行頭の # はコメント。空行は無視されます
OP：お電話ありがとうございます。サポートセンターでございます。
CU：料金プランのことで確認したいことがありまして。
OP：ご契約内容の確認ですね。少々お待ちください。
```

**話者名は自由で、事前に定義する必要はありません。** 名前から、どちら側の音声かを自動で判断します。

| 名前 | 割り当て |
| --- | --- |
| `OP` `OPE` `オペレータ` `担当` `受付` `AGENT` `STAFF` `店員` など | **電話機側**（local） |
| `CU` `CUST` `お客様` `顧客` `カスタマ` `客` `ユーザ` など | **サーバ側**（remote） |

当てはまらない名前は、出てきた順に電話機側 → サーバ側と割り当てます。
変えたいときだけ、GUI の［話者］タブか設定の `speakers` で上書きしてください。

区切りは全角「：」と半角「:」のどちらでも構いません。日本語の話者名も使えます。

```text
オペレータ：いらっしゃいませ。
お客様：これをください。
```

### 通話中の操作

行全体を丸括弧で囲むと指示になります。半角 `( )` でも書けます。

| 書き方 | 意味 |
| --- | --- |
| `（3秒あける）` | 間を空ける。`（3秒）` `（3）` でも同じ |
| `（0.6秒かぶせる）` | 次の発話を直前に食い込ませる（相づち・かぶり）。秒数は省略可 |
| `（保留）` | 保留する（re-INVITE / `a=sendonly`）。`（保留解除）` まで続く |
| `（保留 6秒）` | 6 秒だけ保留する |
| `（保留 OP）` | 掛ける側を指定する |
| `（保留解除）` | 保留を解除する（`a=sendrecv`） |
| `（プッシュ音 CU：1234#）` | DTMF を送る（RFC 2833）。`0-9 * # A-D` が使えます |
| `（音声 OP：./ivr.wav）` | 既存の WAV をそのまま流す |
| `（切断 OP）` | この側から BYE を送る |

指示行には行末コメントも書けます（`（3秒あける）  # 間を置く`）。
発話行の `#` は読み上げ対象なので残ります（`CU：#1番でお願いします。`）。

発話と発話のあいだには既定で `call.default_gap` 秒の間が入り、`（3秒あける）` はそこに加算されます。

### 全体の例

```text
OP：お電話ありがとうございます。サポートセンターの山田でございます。
CU：料金プランのことで確認したいことがありまして。

（0.6秒かぶせる）
OP：はい。

OP：ただいまお調べいたしますので、少々お待ちください。
（保留 6秒）
OP：大変お待たせいたしました。

OP：四桁の暗証番号をダイヤルボタンで入力してください。
（プッシュ音 CU：1234#）

CU：ありがとうございました。
（切断 OP）
```

### 区切り文字を変える

```text
OP>いらっしゃいませ。
お客様|これをください。
```

```bash
python gen_call.py 台本.txt --separators ">|"
```

`[OP] 本文` のような書式は正規表現で指定できます（1 番目が話者、2 番目が本文）。

```bash
python gen_call.py 台本.txt --pattern "^\[(.+?)\]\s*(.*)$"
```

なお、本文中のコロンを話者と読み違えないよう、話者名は 24 文字までとしています。

---

## 2. CSV / Excel から読む

「話者・開始時間・発言内容」の列を持つ CSV / TSV も、そのまま渡せます。
列名から役割を自動で判定するので、**認識結果を書き出した CSV をそのままテスト通話に戻せます。**

```text
話者,開始時間,発言内容
OP,00:00.5,お電話ありがとうございます、サポートセンターの田中でございます。
CU,00:05.0,先日購入した商品の調子が悪いんですが。
OP,00:09.5,ご不便をおかけしております。症状を詳しくお聞かせいただけますか。
```

```bash
python gen_call.py examples/inbound.csv -c examples/config.json
```

**開始時間の列があれば、その時刻に発話を置きます。** 無ければ順に並べます。
時刻は `mm:ss.s` `h:mm:ss.s` 秒数 のいずれでも読めます。

自動判定に使う列名（部分一致）:

| 用途 | 列名 |
| --- | --- |
| 話者 | `音声のチャンネル種類` / `話者` / `チャンネル` / `区分` / `speaker` / `channel` / `ch` / `role` |
| 本文 | `発言内容(認識結果)` / `発言内容` / `発話内容` / `テキスト` / `内容` / `本文` / `text` / `utterance` |
| 開始時間 | `開始時間(最新版数)` / `開始時間` / `開始` / `start` / `begin` |

列名が分からないときは、まず中身を確認できます。

```bash
python gen_call.py 通話ログ.csv --show-columns
```

```
区切り文字: カンマ
先頭行: 見出し
列一覧:
  1 列目: 通話ID
  2 列目: 開始時間(最新版数)  <- 開始時間 として自動判定
  3 列目: 音声のチャンネル種類  <- 話者 として自動判定
  4 列目: 発言内容(認識結果)  <- 本文 として自動判定
```

自動判定に任せない場合は、列名でも列番号でも指定できます。

```bash
python gen_call.py log.tsv --delimiter $'\t' --no-header \
    --speaker-column 1 --start-column 2 --text-column 3
python gen_call.py 通話ログ.csv --no-timings      # 開始時間を使わず順に並べる
```

> 複数通話ぶんが 1 つの CSV に入っていて、通話ごとに開始時間がリセットされる場合は、
> 通話 ID で行を絞ってから渡してください。絞らないと時刻が巻き戻って発話が重なります。

---

## 3. GUI で使う

### 実行ファイルを受け取って使う（Python は不要）

[**Releases**](../../releases) から
`SipCallGenerator.exe` をダウンロードして、ダブルクリックするだけです。
インストールも、Python も、追加のライブラリも要りません。

> 実行ファイルはリポジトリには入れていません（バイナリを git に置くとクローンが重くなるため）。
> 配布物は Releases に置いてあります。

### ソースから起動する

クローンしたフォルダで、そのまま動きます。追加のインストールは不要です。

```bash
python sip_gui.pyw
```

| タブ | 中身 |
| --- | --- |
| **通話原稿** | 台本の編集。読み込み・保存、書き方のヘルプ |
| **接続設定** | SIP サーバ / 電話機の IP・ポート、From / To、呼の向き、コーデック、保留の方式、ACK に足すヘッダ、劣化テスト用のジッタ・ロス |
| **話者** | 台本から拾った話者と、その割り当て。変えたいときだけ上書き |
| **取り込み** | 生成後に音声認識サービスを入れ直す設定 |

下部で出力先を指定して［pcap を生成］を押すと、進捗と結果がログ欄に出ます。
生成時には、台本から拾った話者がどちら側になったかも表示されます。
［検証］で中身のチェック、［フォルダを開く］で出力先を開けます。

［ファイル］メニューから設定を JSON で保存でき、その JSON は `gen_call.py -c` に
そのまま渡せます。**GUI で作った設定を CLI のバッチに回す**、という使い方ができます。

### 配布用の実行ファイルを自分で作る

Releases のものを使わず、手元で作り直す場合です。

```bash
python -m pip install pyinstaller
python build_exe.py
```

`dist/` に 2 つできます。どちらも配布先に Python が要りません。

| ファイル | 用途 |
| --- | --- |
| `SipCallGenerator.exe` | GUI 版（コンソールなし） |
| `gen_call.exe` | CLI 版（バッチから呼ぶ用） |

PyInstaller は**作るときだけ**必要で、出来上がった exe の実行には要りません。

---

## 4. コマンドラインで使う

```bash
python gen_call.py examples/inbound.txt -c examples/config.json -o out/inbound.pcap
```

```
台本: examples/inbound.txt（22 行）
話者: OP → 電話機側 / CU → サーバ側

pcap を書き出しました: out/inbound.pcap
  Call-ID    : a3b1799d-192-168-10-1
  向き       : inbound  ("山田 花子" <sip:0312345678@pbx.example.local> → "サポート受付" <sip:1001@pbx.example.local>)
  コーデック : PCMU / 20ms  RTP 192.168.10.50:40000 ⇄ 192.168.10.1:30000
  通話長     : 95.3 秒 (うち音声 92.4 秒)
  パケット   : 合計 8971 (SIP 13 / RTP 8918 / DTMF 40)
  保留       : 1 回 (39.0-45.0秒)
  正解データ : out/inbound.json
```

設定ファイルを作らず、コマンドラインだけでも指定できます。

```bash
python gen_call.py 台本.txt \
    --server-ip 10.0.0.1 --client-ip 10.0.0.50 \
    --from 0312345678 --to 1001 --direction inbound \
    -o out/test.pcap
```

主なオプション:

| オプション | 意味 |
| --- | --- |
| `-c, --config` | 設定 JSON |
| `-o, --output` | 出力する pcap（既定: 台本と同名の `.pcap`） |
| `--transcript` | 正解データ JSON の出力先（`none` で出力しない） |
| `--export-wav 接頭辞` | RTP に載せた音を確認用 WAV で書き出す |
| `--seed 42` | Call-ID・タグ・SSRC を再現可能にする |
| `--start-time` | pcap の開始時刻（ISO8601） |
| `--separators` / `--pattern` | 台本の区切り文字 / 正規表現 |
| `--delimiter` / `--speaker-column` / `--text-column` / `--start-column` | CSV の列指定 |
| `--no-header` / `--no-timings` / `--show-columns` | CSV の読み方 |
| `--ack-header` / `--sip-header` | SIP ヘッダの追加（→ 6 節） |
| `--restart-service` | 生成後に音声認識サービスを入れ直して取り込ませる（→ 9 節） |
| `--list-voices` | この PC で使える合成音声の一覧 |
| `--init-config PATH` | 設定 JSON のひな形を書き出す |

---

## 5. 設定ファイル

`python gen_call.py --init-config myconfig.json` でひな形が出ます。
最低限、次の 4 つを埋めれば動きます。**話者の定義は要りません**（台本から拾います）。

```jsonc
{
  "direction": "inbound",                       // inbound=着信 / outbound=発信
  "sip_server": { "ip": "192.168.10.1" },       // SIP サーバ（PBX）の IP
  "client":     { "ip": "192.168.10.50" },      // クライアント電話機の IP
  "from": { "user": "0312345678" },             // From ヘッダ（発信者）
  "to":   { "user": "1001" }                    // To ヘッダ（内線番号）
}
```

### 主な項目

| 項目 | 既定 | 説明 |
| --- | --- | --- |
| `direction` | `inbound` | `inbound` はサーバが INVITE を出し電話機が受ける（受電）。`outbound` はその逆 |
| `sip_server.ip` / `.port` / `.rtp_port` / `.domain` | `192.168.10.1` / 5060 / 30000 / IP と同じ | SIP サーバ側 |
| `client.ip` / `.port` / `.rtp_port` | `192.168.10.50` / 5060 / 40000 | 電話機側 |
| `from` / `to` | — | **SIP の From / To ヘッダそのもの**。`user` と `display`（表示名） |
| `codec` | `PCMU` | `PCMU`(μ-law) か `PCMA`(A-law) |
| `ptime` | `20` | RTP 1 パケットのミリ秒 |
| `script.separators` | `:：` | 「話者：本文」の区切り文字 |
| `script.pattern` | `null` | 話者プレフィックスの正規表現 |
| `speakers` | `{}` | **上書きだけ**。書かなくても台本から推定される |
| `call.ring_seconds` | `2.0` | 180 Ringing から 200 OK まで |
| `call.hangup_by` | `local` | 既定でどちらが切るか |
| `call.default_gap` | `0.4` | 発話間の既定の間合い（秒） |
| `call.start_time` | 現在時刻 | pcap の先頭時刻（ISO8601） |
| `sip_headers` | `{"ACK": []}` | SIP メッセージに足すヘッダ（→ 6 節） |
| `hold.mode` | `sendonly` | `sendonly`=保留した側は送出継続 / `inactive`=双方停止 |
| `hold.media` | `null` | 保留中に流す WAV（保留音）。未指定なら無音 |
| `dtmf.payload_type` | `101` | telephone-event のペイロードタイプ |
| `media.silence_mode` | `continuous` | `continuous`=無音区間も送る / `suppress`=送らない |
| `network.jitter_ms` / `packet_loss` | `0` | 送出時刻のゆらぎ・パケットロス率（劣化系のテスト用） |

### From / To の向きについて

`from` / `to` は **SIP ヘッダそのまま**なので、`direction` に合わせて中身を入れ替えてください。

* `inbound`（受電）: `from` = 発信者番号 `0312345678`、`to` = 内線 `1001`
* `outbound`（発信）: `from` = 内線 `1001`、`to` = 相手番号 `0120999888`

### 話者を上書きする

```bash
python gen_call.py --list-voices
```

日本語の声が Haruka 1 つしかない環境が多いので、既定では電話機側とサーバ側で
ピッチと話速を変えて聞き分けられるようにしています。変えたいときだけ書きます。

```jsonc
"speakers": {
  "IVR": { "side": "remote", "voice": "Haruka", "rate": -2, "pitch": "-20%" }
}
```

`rate` は SAPI の速度（-10〜10 の整数）、`pitch` は SSML の `prosody pitch`（`"+20%"` など）です。
より自然な声が必要なら、外部で用意した WAV を `（音声 OP：./a.wav）` で流し込んでください。

---

## 6. SIP ヘッダを足す

認識エンジン側が独自ヘッダを見る場合に備えて、**任意のヘッダを足したり差し替えたり**できます。

```jsonc
"sip_headers": {
  "ACK": [
    "X-Call-Id: {call_id}",
    "X-Agent-Extension: 1001",
    "User-Agent: MyPhone/2.0"
  ]
}
```

```bash
python gen_call.py 台本.txt --ack-header "X-Call-Id: {call_id}" --ack-header "X-Agent: 1001"
```

GUI では［接続設定］タブの「ACK に足すヘッダ」に 1 行ずつ書きます。

**キー**はメソッド名（`INVITE` `ACK` `BYE` …）か応答コード（`"200"` `"180"` …）、
`"*"` はすべてのメッセージに適用されます。

```jsonc
"sip_headers": {
  "*":      ["X-Test-Run: run-001"],
  "INVITE": ["P-Asserted-Identity: <sip:{caller_user}@example.local>"],
  "ACK":    ["X-Call-Id: {call_id}"],
  "200":    ["X-Answered: yes"]
}
```

ACK 以外を CLI から指定するときは、先頭にキーを付けます。

```bash
python gen_call.py 台本.txt --sip-header "INVITE:X-Foo: bar" --sip-header "*:X-Run: 001"
```

**同じ名前のヘッダが既にあれば差し替わります**（重複しません）。
値を `null` にすると、そのヘッダを取り除きます。

```jsonc
"sip_headers": { "ACK": { "User-Agent": null } }
```

値には次の項目を差し込めます。`{local_*}` はそのメッセージを送る側、`{remote_*}` は相手側です。

`{call_id}` `{from_tag}` `{to_tag}` `{branch}` `{cseq}` `{method}` `{code}`
`{local_ip}` `{local_port}` `{local_user}` `{remote_ip}` `{remote_port}` `{remote_user}`
`{caller_user}` `{callee_user}`

### 変えられないヘッダ

**`From` `To` `Call-ID` `CSeq` `Via` はここでは変えられません。**
呼を識別する要素で、書き換えるとダイアログとして成立しなくなるためです。
`From` / `To` は設定の `from` / `to` で、それ以外は生成側が一貫性を保って振っています。
指定するとエラーで止まります。

---

## 7. 正解データ（`*.json`）

pcap と同時に、認識結果の答え合わせに使える JSON が出ます。

```jsonc
{
  "call": { "call_id": "...", "direction": "inbound", "from": "...", "to": "...",
            "media_start": "2026-09-10T10:00:02.850+09:00", "media_duration": 92.412 },
  "streams": {
    "local":  { "ip": "192.168.10.50", "port": 40000, "ssrc": "0x07a0ca6e" },
    "remote": { "ip": "192.168.10.1",  "port": 30000, "ssrc": "0x5ae1dbad" }
  },
  "utterances": [
    { "index": 0, "speaker": "OP", "side": "local",
      "start": 0.0, "end": 5.574, "abs_start": "2026-09-10T10:00:02.850+09:00",
      "text": "お電話ありがとうございます。…" }
  ],
  "events": [ { "type": "hold", "by": "local", "start": 38.953, "end": 44.953 },
              { "type": "dtmf", "side": "remote", "digits": "1234#", "start": 73.869 } ],
  "sip":    [ { "time": 0.0, "message": "INVITE sip:1001@pbx.example.local SIP/2.0" } ]
}
```

`start` / `end` は音声開始（ACK + `answer_delay`）からの相対秒、`abs_start` は pcap 上の絶対時刻です。

---

## 8. 生成した pcap の確認

Wireshark を開かずに中身を検証できます。

```bash
python verify_pcap.py out/inbound.pcap
python verify_pcap.py out/inbound.pcap --extract out/rx   # RTP を WAV に戻す
```

SIP のメッセージ列と SDP の方向（`sendrecv` / `sendonly`）、RTP のシーケンス連続性・
タイムスタンプ・チェックサムをまとめて確認し、問題があれば列挙します。

Wireshark で見る場合は **電話 → VoIP 通話** で通話として認識され、
**電話 → RTP → RTP ストリーム** から音声を再生できます。

---

## 9. 音声認識サービスに取り込ませる

pcap を生成したあと、認識側のサービスを**停止 → 開始パラメータに pcap のパスを付けて開始**
し直すところまで、続けて実行できます。

```bash
python gen_call.py examples/inbound.txt -c examples/config.json -o out/inbound.pcap --restart-service
```

```
サービスを入れ直しています: AmiVoiceRealTimeRecorder
  AmiVoiceRealTimeRecorder を停止しています (現在: Running)…
  停止しました
  AmiVoiceRealTimeRecorder を開始しています…
    開始パラメータ: --callid-generate --packet-sync C:\...\out\inbound.pcap
  開始しました
```

`{pcap}` が **pcap の絶対パス**に置き換わります。

### 既にある pcap を読み込ませるだけ

生成せずに、手元の pcap を読み込ませるだけの使い方もできます。

**GUI**: ［取り込み］タブで pcap を選んで［この pcap を取り込ませる］を押すだけです。

* ［参照…］で任意の pcap を選べます
* 生成した直後なら、そのパスが自動で入っています（［生成した pcap を使う］でも入ります）
* **開始パラメータは pcap のパス以外は固定**で、画面には実際に発行される内容がそのまま出ます

**実行ファイルに pcap を渡しても開けます。** ドラッグ＆ドロップでも、引数でも構いません。

```bash
SipCallGenerator.exe D:\calls\call.pcap
```

`.pcap` を渡すと［取り込み］タブに入り、それ以外のファイルは台本として開きます。

**コマンドライン**:

```bash
python feed_pcap.py out/inbound.pcap            # 停止 → 開始
python feed_pcap.py --status                    # 状態だけ見る
python feed_pcap.py out/inbound.pcap --dry-run  # 実行せず、発行するコマンドを表示
```

### 設定

| 設定 (`service`) | 既定値 | 説明 |
| --- | --- | --- |
| `enabled` | `false` | `--restart-service` を付けるか、ここを `true` にすると生成後に続けて実行する |
| `name` | `AmiVoiceRealTimeRecorder` | 対象のサービス名 |
| `start_args` | `--callid-generate --packet-sync {pcap}` | 開始パラメータ。**GUI からは変えられません**（打ち間違いで取り込みが失敗しないよう固定）。バッチ用途で変える必要があるときだけ、設定ファイルかコマンドラインで指定します |
| `stop_timeout` / `start_timeout` | `30` | 状態が変わるのを待つ秒数 |

開始パラメータをどうしても変える場合は、**値が `-` で始まるので `=` でつないでください**。

```bash
python gen_call.py 台本.txt --restart-service --service-name MyRecorder \
    --service-args="--callid-generate --packet-sync {pcap}"
python feed_pcap.py call.pcap --args="--packet-sync {pcap}"
```

### 注意

* **管理者権限が必要です。** 権限がない場合は、そのまま貼って使える `sc stop` / `sc start`
  のコマンドを表示して終了します（pcap の生成自体は完了しています）。
* 開始パラメータは services.msc の［開始パラメーター］欄と同じ意味で、
  `sc start <サービス名> <パラメータ>` として渡されます。

---

## 10. 実際にネットワークへ流す

pcap をそのまま投げるなら [tcpreplay](https://tcpreplay.appneta.com/) を使います。

```bash
tcpreplay -i eth0 out/inbound.pcap          # 記録された間隔どおりに送出
tcpreplay -i eth0 --topspeed out/inbound.pcap
```

IP や MAC を実環境に合わせる場合は、設定ファイル側で最初から実環境の値にしておくのが確実です
（`tcprewrite` で後から書き換えると、チェックサムの再計算が必要になります）。

---

## 11. よく使う組み合わせ

```bash
# 認識結果の CSV から、同じ通話を作り直す
python gen_call.py Recognition.csv -c config.json -o out/replay.pcap

# 回線品質が悪いときの認識精度を見る
#   設定に "network": { "jitter_ms": 20, "packet_loss": 0.02 }

# 無音区間を送らない実装を模擬する
#   設定に "media": { "silence_mode": "suppress" }

# A-law の環境向け
python gen_call.py 台本.txt -c config.json --codec PCMA

# 同じ内容を毎回まったく同じ Call-ID / SSRC で作る（回帰テスト用）
python gen_call.py 台本.txt -c config.json --seed 42
```

---

## 12. 構成

```
sip_gui.pyw          GUI の起動口
build_exe.py         配布用の実行ファイルを作る（PyInstaller）
gen_call.py          コマンドライン（生成）
verify_pcap.py       コマンドライン（検証・要約・WAV 抽出）
feed_pcap.py         コマンドライン（既存 pcap を認識サービスに取り込ませる）
sipgen/
  config.py          設定の読み込みと既定値
  script.py          台本（話者：本文）のパース
  tabular.py         CSV / TSV のパースと列の自動判定
  loader.py          台本の読み込み口（txt / CSV の振り分け）
  speakers.py        話者名から local / remote を推定
  tts.py             Windows SAPI での音声合成（speak.ps1 を呼ぶ）
  speak.ps1          8kHz/16bit/モノラルで WAV を書き出す PowerShell
  audio.py           WAV 読み書き・リサンプル・G.711 エンコード
  sipmsg.py          SIP メッセージと SDP、ダイアログ状態、追加ヘッダ
  rtp.py             RTP パケットと RFC 2833 DTMF
  pcapw.py           Ethernet/IPv4/UDP の組み立てと pcap 書き出し
  builder.py         台本 → タイムライン → パケットの中核
  gui.py             tkinter の画面
  proc.py            外部コマンド呼び出し（コンソール窓を出さない）
examples/            サンプルの台本（txt / csv）と設定
tests/               受け入れテスト
```

合成した音声は台本と同じ場所の `.tts-cache/`（GUI では `%LOCALAPPDATA%`）に
キャッシュされます。同じセリフを何度生成しても、2 回目以降は合成し直しません。

### テスト

```bash
python tests/test_generate.py
```

音声合成に依存しない形（サイン波の WAV）で 1 通話を組み立て、pcap を読み直して
SIP ダイアログの流れ、保留中に音声が止まること、DTMF イベント、RTP の連番と
タイムスタンプ、チェックサム、追加ヘッダ、正解データと実際の音の一致まで検証します。
GUI の設定変換もウィンドウを出さずに確認します（tkinter や画面がない環境では
その部分だけ自動的に飛ばします）。

---

## 制約

* 音声は G.711（PCMU / PCMA）のみです。G.729 や Opus には対応していません
* SIP は UDP のみです（TCP / TLS 非対応）。SRTP も扱いません
* REGISTER・認証（401/407）・転送（REFER）・早期メディア（183）は生成しません
* 音声合成とサービス操作、および配布用の実行ファイルは Windows 専用です。
  他の OS では `（音声 OP：a.wav）` で音声を持ち込めば CLI が動きます
  （pcap の組み立て自体は OS に依存しません）
* 以前の `@hold` / `@wait` 形式の台本もそのまま読めます
