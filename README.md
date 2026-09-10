# SIP/RTP テスト通話ジェネレータ

通話の原稿（テキスト）から、**SIP のシグナリングと RTP の音声が入った pcap** を作ります。
コールセンター向けの音声認識システムのように「呼制御を SIP から読み、音声を RTP から認識する」
仕組みに、実機や実回線なしでテスト通話を流し込むためのツールです。

```
原稿 (.txt)  ──┬─→  Windows の音声合成  ──→  8kHz G.711  ──→  RTP  ──┐
               │                                                      ├─→  call.pcap
設定 (.json) ──┴─→  SIP ダイアログ (INVITE / 保留 / BYE)  ───────────┘
                                                                      └─→  call.json（正解データ）
```

* Python 3.8 以降だけで動きます（**追加ライブラリのインストールは不要**）
* 音声合成は Windows 標準の SAPI（System.Speech）を使うので、**オフラインで完結**します
* 生成と同時に、発話内容と時刻を記録した **正解データ JSON** が出るので、認識結果の突き合わせにそのまま使えます

---

## 1. 使い方

```bash
python gen_call.py examples/inbound.txt -c examples/config.json -o out/inbound.pcap
```

```
pcap を書き出しました: out/inbound.pcap
  Call-ID    : a3b1799d-192-168-10-1
  向き       : inbound  ("山田 花子" <sip:0312345678@pbx.example.local> → "サポート受付" <sip:1001@pbx.example.local>)
  コーデック : PCMU / 20ms  RTP 192.168.10.50:40000 ⇄ 192.168.10.1:30000
  通話長     : 95.3 秒 (うち音声 92.4 秒)
  パケット   : 合計 8969 (SIP 13 / RTP 8916 / DTMF 40)
  保留       : 1 回 (39.0-45.0秒)
  正解データ : out/inbound.json
```

設定ファイルを作らず、コマンドラインだけでも指定できます。

```bash
python gen_call.py script.txt \
    --server-ip 10.0.0.1 --client-ip 10.0.0.50 \
    --from 0312345678 --to 1001 --direction inbound \
    -o out/test.pcap
```

主なオプション:

| オプション | 意味 |
| --- | --- |
| `-c, --config` | 設定 JSON |
| `-o, --output` | 出力する pcap（既定: 原稿と同名の `.pcap`） |
| `--transcript` | 正解データ JSON の出力先（`none` で出力しない） |
| `--export-wav 接頭辞` | RTP に載せた音を確認用 WAV で書き出す |
| `--seed 42` | Call-ID・タグ・SSRC を再現可能にする |
| `--start-time` | pcap の開始時刻（ISO8601） |
| `--list-voices` | この PC で使える合成音声の一覧 |
| `--init-config PATH` | 設定 JSON のひな形を書き出す |
| `--restart-service` | 生成後に音声認識サービスを入れ直して取り込ませる（→ [6](#6-音声認識サービスに取り込ませる)） |

---

## 2. 原稿の書き方

```text
# 行頭の # はコメント

agent: お電話ありがとうございます。サポートセンターの山田でございます。
customer: 料金プランのことで確認したいことがありまして。

@wait -0.6            # 負の値 = 直前の発話に食い気味に重ねる（相づち・かぶり）
agent: はい。

@hold                 # 保留開始。re-INVITE (a=sendonly) が入る
@wait 6               # 保留のまま 6 秒
@unhold               # 保留解除。re-INVITE (a=sendrecv)

@dtmf customer: 1234# # RFC 2833 の DTMF
@wav agent: ./ivr.wav # 既存の WAV をそのまま流す
@hangup agent         # この側から BYE を送る
```

| 書き方 | 意味 |
| --- | --- |
| `話者: セリフ` | その話者の音声を合成して流す |
| `@wait 秒` | 間を空ける。**負の値で直前の発話に重なる** |
| `@hold` / `@unhold` | 保留・保留解除。`@hold agent` のように掛ける側を書ける |
| `@dtmf 話者: 桁` | DTMF。`0-9 * # A-D` が使える |
| `@wav 話者: パス` | 既存 WAV を流す（原稿からの相対パス可） |
| `@hangup 話者` | どちら側が BYE を送るか |

話者名（`agent` / `customer` など）は設定の `speakers` で定義します。
発話と発話のあいだには既定で `call.default_gap` 秒の間が入り、`@wait` はそこに加算されます。

---

## 3. 設定ファイル

`python gen_call.py --init-config myconfig.json` でひな形が出ます。
最低限、次の 4 つを埋めれば動きます。

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
| `from` / `to` | — | **SIP の From / To ヘッダそのもの**。`user` と `display`（表示名）を持つ |
| `codec` | `PCMU` | `PCMU`(μ-law) か `PCMA`(A-law) |
| `ptime` | `20` | RTP 1 パケットのミリ秒 |
| `call.ring_seconds` | `2.0` | 180 Ringing から 200 OK まで |
| `call.hangup_by` | `local` | 既定でどちらが切るか（`local`=電話機 / `remote`=サーバ） |
| `call.default_gap` | `0.4` | 発話間の既定の間合い（秒） |
| `call.start_time` | 現在時刻 | pcap の先頭時刻（ISO8601） |
| `speakers.<名前>.side` | — | `local`(電話機側) か `remote`(サーバ側)。**どちらの RTP に載せるか** |
| `speakers.<名前>.voice` / `rate` / `pitch` | — | 合成音声の指定。声が 1 種類しかない環境では `pitch` で話者を作り分ける |
| `hold.mode` | `sendonly` | `sendonly`=保留した側は送出継続 / `inactive`=双方停止 |
| `hold.media` | `null` | 保留中に流す WAV（保留音）。未指定なら無音 |
| `dtmf.payload_type` | `101` | telephone-event のペイロードタイプ |
| `media.silence_mode` | `continuous` | `continuous`=無音区間も送る / `suppress`=送らない |
| `network.jitter_ms` / `packet_loss` | `0` | 送出時刻のゆらぎ・パケットロス率（劣化系のテスト用） |

### From / To の向きについて

`from` / `to` は **SIP ヘッダそのまま**なので、`direction` に合わせて中身を入れ替えてください。

* `inbound`（受電）: `from` = 発信者番号 `0312345678`、`to` = 内線 `1001`
* `outbound`（発信）: `from` = 内線 `1001`、`to` = 相手番号 `0120999888`

### 話者の作り分け

```bash
python gen_call.py --list-voices
```

日本語の声が Haruka 1 つしかない環境が多いので、既定の設定では `pitch` を変えて
オペレーターとお客様を聞き分けられるようにしています。

```jsonc
"speakers": {
  "agent":    { "side": "local",  "voice": "Haruka", "rate": 0,  "pitch": "-8%" },
  "customer": { "side": "remote", "voice": "Haruka", "rate": -1, "pitch": "+20%" }
}
```

`rate` は SAPI の速度（-10〜10 の整数）、`pitch` は SSML の `prosody pitch`（`"+20%"` など）です。
より自然な声が必要なら、外部で用意した WAV を `@wav` で流し込んでください。

---

## 4. 正解データ（`*.json`）

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
    { "index": 0, "speaker": "agent", "side": "local",
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

## 5. 生成した pcap の確認

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

## 6. 音声認識サービスに取り込ませる

pcap を生成したあと、認識側のサービスを**停止 → 開始パラメータに pcap のパスを付けて開始**
し直すところまで、続けて実行できます。

```bash
python gen_call.py examples/inbound.txt -c examples/config.json -o out/inbound.pcap --restart-service
```

```
pcap を書き出しました: out/inbound.pcap
  …

サービスを入れ直しています: AmiVoiceRealTimeRecorder
  AmiVoiceRealTimeRecorder を停止しています (現在: Running)…
  停止しました
  AmiVoiceRealTimeRecorder を開始しています…
    開始パラメータ: --callid-generate --packet-sync C:\...\out\inbound.pcap
  開始しました
認識サービスに取り込ませました: AmiVoiceRealTimeRecorder --callid-generate --packet-sync C:\...\out\inbound.pcap
```

`{pcap}` が **生成した pcap の絶対パス**に置き換わります。既定値は次のとおりで、
サービス名も開始パラメータも自由に変えられます。

| 設定 (`service`) | 既定値 | 説明 |
| --- | --- | --- |
| `enabled` | `false` | `--restart-service` を付けるか、ここを `true` にすると実行する |
| `name` | `AmiVoiceRealTimeRecorder` | 対象のサービス名 |
| `start_args` | `--callid-generate --packet-sync {pcap}` | 開始パラメータ。`{pcap}` は必須 |
| `stop_timeout` / `start_timeout` | `30` | 状態が変わるのを待つ秒数 |

コマンドラインからも変えられます。**値が `-` で始まるので `=` でつないでください**
（`--service-args "..."` の形だと argparse が別のオプションと誤認します）。

```bash
python gen_call.py script.txt --restart-service \
    --service-name MyRecorder \
    --service-args="--callid-generate --packet-sync {pcap}"
```

### 既存の pcap をもう一度流す

生成し直さずに、同じ pcap を再度取り込ませるだけなら `feed_pcap.py` を使います。

```bash
python feed_pcap.py out/inbound.pcap            # 停止 → 開始
python feed_pcap.py --status                    # 状態だけ見る
python feed_pcap.py out/inbound.pcap --dry-run  # 実行せず、発行するコマンドを表示
```

```
$ python feed_pcap.py out/inbound.pcap --dry-run
sc stop AmiVoiceRealTimeRecorder
sc start AmiVoiceRealTimeRecorder --callid-generate --packet-sync C:\...\out\inbound.pcap
```

### 注意

* **管理者権限が必要です。** 管理者として開いたターミナルから実行してください。
  権限がない場合は、そのまま貼って使える `sc stop` / `sc start` のコマンドを表示して終了します
  （pcap の生成自体は完了しています）。
* 開始パラメータは services.msc の［開始パラメーター］欄と同じ意味で、
  `sc start <サービス名> <パラメータ>` として渡されます。
* pcap のパスに空白が含まれていても、`{pcap}` は 1 つの引数として渡されます。

---

## 7. 実際にネットワークへ流す

pcap をそのまま投げるなら [tcpreplay](https://tcpreplay.appneta.com/) を使います。

```bash
tcpreplay -i eth0 out/inbound.pcap          # 記録された間隔どおりに送出
tcpreplay -i eth0 --topspeed out/inbound.pcap
```

IP や MAC を実環境に合わせる場合は、設定ファイル側で最初から実環境の値にしておくのが確実です
（`tcprewrite` で後から書き換えると、チェックサムの再計算が必要になります）。

---

## 8. よく使う組み合わせ

```bash
# 保留音を鳴らしながら保留する
#   設定に "hold": { "mode": "sendonly", "media": "./moh.wav" }

# 回線品質が悪いときの認識精度を見る
#   設定に "network": { "jitter_ms": 20, "packet_loss": 0.02 }

# 無音区間を送らない実装を模擬する
#   設定に "media": { "silence_mode": "suppress" }

# A-law の環境向け
python gen_call.py script.txt -c config.json --codec PCMA

# 同じ内容を毎回まったく同じ Call-ID / SSRC で作る（回帰テスト用）
python gen_call.py script.txt -c config.json --seed 42
```

---

## 9. 構成

```
gen_call.py          コマンドライン（生成）
verify_pcap.py       コマンドライン（検証・要約・WAV 抽出）
feed_pcap.py         コマンドライン（既存 pcap を認識サービスに取り込ませる）
sipgen/
  config.py          設定の読み込みと既定値
  script.py          原稿のパース
  tts.py             Windows SAPI での音声合成（speak.ps1 を呼ぶ）
  speak.ps1          8kHz/16bit/モノラルで WAV を書き出す PowerShell
  audio.py           WAV 読み書き・リサンプル・G.711 エンコード
  sipmsg.py          SIP メッセージと SDP、ダイアログ状態
  rtp.py             RTP パケットと RFC 2833 DTMF
  pcapw.py           Ethernet/IPv4/UDP の組み立てと pcap 書き出し
  builder.py         原稿 → タイムライン → パケットの中核
  service.py         Windows サービスの停止・開始（開始パラメータ付き）
examples/            サンプルの原稿と設定
tests/               受け入れテスト
```

合成した音声は原稿と同じ場所の `.tts-cache/` にキャッシュされます。
同じセリフを何度生成しても、2 回目以降は合成し直しません。

### テスト

```bash
python tests/test_generate.py
```

音声合成に依存しない形（サイン波の WAV）で 1 通話を組み立て、pcap を読み直して
SIP ダイアログの流れ、保留中に音声が止まること、DTMF イベント、RTP の連番と
タイムスタンプ、チェックサム、正解データと実際の音の一致まで検証します。

---

## 制約

* 音声は G.711（PCMU / PCMA）のみです。G.729 や Opus には対応していません
* SIP は UDP のみです（TCP / TLS 非対応）。SRTP も扱いません
* REGISTER・認証（401/407）・転送（REFER）・早期メディア（183）は生成しません
* 音声合成は Windows 専用です。他の OS では `@wav` で音声を持ち込んでください
  （pcap の組み立て自体は OS に依存しません）
