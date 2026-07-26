# news-tweet-bot

興味のあるトピックのホットな記事を毎朝集め、Claude に要約させて 9:30 JST に X へ投稿する。
X API は**自分では契約しない**。Buffer に予約を積み、X への配信は Buffer に任せる。

```
RSS + はてブ検索 ─▶ 既出除外 ─▶ Claude が選定＆要約 ─▶ Buffer に予約 ─▶ 9:30 に X へ
   collectors.py      main.py       summarize.py      poster_buffer.py     (Buffer)
```

## なぜ Buffer 経由か

X は2026年2月に無料枠を廃止し、従量課金に移行した。リンクを含む投稿は1件 $0.20 かかる。
Buffer は自社で X API の契約を持っているので、Buffer の無料プラン（3チャンネル／各10予約枠）
に乗れば、こちらの X API 費用はゼロになる。1日1本ならこの枠で足りる。

副次的な効果として、**投稿時刻の精度問題も消える**。GitHub Actions の cron は数分〜数十分
遅れるが、この構成ではジョブは「7時ごろに起きて 9:30 の予約を積む」だけなので、
多少遅れても配信時刻は 9:30 のままになる。

かかるのは Claude API のみで、1日1リクエストなので月 $0.1 未満。

## セットアップ

### 1. Buffer 側

1. Buffer に登録し、X アカウントをチャンネルとして接続する
2. [Settings → API](https://publish.buffer.com/settings/api) で API キーを発行する
   （キーはオーナー権限のアカウントでのみ発行できる）
3. Buffer の投稿スケジュールでキューが停止していないか確認する

### 2. GitHub 側

Settings → Secrets and variables → Actions に登録：

| 種別 | 名前 | 必須 |
|---|---|---|
| Secret | `ANTHROPIC_API_KEY` | ○ |
| Secret | `BUFFER_API_KEY` | ○ |
| Variable | `BUFFER_CHANNEL_ID` | 任意（指定すると解決用の2リクエストを省略） |

`BUFFER_CHANNEL_ID` は次のコマンドで確認できる：

```bash
BUFFER_API_KEY=... python src/main.py --channels
```

### 3. トピックを書く

`config.yaml` の `topics` を自分の興味に置き換える。`feeds` は信頼できる媒体の RSS、
`keywords` ははてなブックマーク検索に投げる語。前者で一次情報を網羅し、
後者のブクマ数で話題性を測る、という役割分担になっている。

### 4. 試す

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python src/main.py --dry-run
```

`--dry-run` なら Buffer にも X にも何も送らず、選ばれた記事・本文・予約先の時刻だけを出す。
Buffer のキーも不要なので、まずこれでトピック設定とトーンを詰めるとよい。

### 5. 有効化

workflow を push すると cron が動きはじめる。最初は Actions タブから
`workflow_dispatch` → dry_run = true で手動実行して確認する。

## 動作の詳細

- **予約時刻**: `config.yaml` の `schedule_at`（既定 09:30 JST）。実行時点でその時刻を
  過ぎていれば自動的に翌日ぶんとして予約される。`posts_per_run` が2以上なら 30 分ずつずらす。
- **重複投稿の防止**: `state/posted.json` に投稿済み URL のハッシュを 60 日分保存し、
  ワークフローが自動でコミットして戻す。ローカル実行と Actions 実行を混ぜると
  コンフリクトするので、どちらかに寄せるのが無難。
- **文字数**: X は全角2・半角1でカウントし上限280（＝全角140字）。URL は実際の長さに
  関わらず一律23文字。`poster.py` の `weighted_length()` で予約前に検証している。
- **GraphQL の組み立て**: `createPost` の input 型名が変わっても壊れないよう、GraphQL 変数
  ではなくリテラル埋め込みにしている。エスケープは `json.dumps` が GraphQL 互換なのでそれを使う。

## 注意点

- Buffer の公開 API は **2026年2月開始のパブリックベータ**。仕様変更がありうる
  （例: 2026年5月25日にメディア添付の形式が変わり、旧形式が動かなくなった）。
  変更は [Changelog](https://developers.buffer.com/changelog.html) で追える。
- 無料プランのレート制限は 24 時間あたり 100 リクエスト程度。1日1〜3本なら余裕。
- Buffer 側でキューが停止していると予約は積まれても配信されない。スクリプトは警告を出す。
- 自動投稿である旨をプロフィールに書き、引用元リンクは必ず残すこと。

## X API に直接投稿したい場合

`poster.py` が残してある。`main.py` の `poster_buffer.publish(...)` を
`poster.post(...)` に差し替え、`requirements.txt` の `requests-oauthlib` を有効化し、
`X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` を渡す。
この場合は cron の遅延対策が別途必要になる（早めに起動して目標時刻まで sleep する等）。

## 拡張のアイデア

- Buffer は Bluesky / Mastodon / LinkedIn も同じ `createPost` で扱えるので、
  `channelId` を増やすだけでクロス投稿にできる
- 運用初期は Buffer の下書き（`createPost` の draft モード）に積んで、目視確認してから
  手で公開する、という半自動運用にするとミスが表に出ない
