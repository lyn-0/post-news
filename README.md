# news-tweet-bot

興味のあるトピックのホットな記事を毎朝集め、9:30 JST に X へ投稿する。
**API キーは Buffer のものだけ**。LLM は使わず、スコアリングで記事を選ぶ。

```
RSS + Qiita API ─▶ 既出除外 ─▶ スコアで選定 ─▶ Buffer に予約 ─▶ 9:30 に X へ
   collectors.py      main.py     selector.py    poster_buffer.py    (Buffer)
```

投稿文は記事タイトルをそのまま使う。技術ニュースはタイトルが結論になっていることが
多いので、これで実用に足りる。要約が欲しくなったら `selector.py` の `build_text()` を
差し替えれば、あとから LLM を足せる。

## なぜ Buffer 経由か

X は2026年2月に無料枠を廃止し、従量課金に移行した。リンクを含む投稿は1件 $0.20 かかる。
Buffer は自社で X API の契約を持っているので、Buffer の無料プラン（3チャンネル／各10予約枠）
に乗れば、こちらの X API 費用はゼロになる。1日1本ならこの枠で足りる。

副次的な効果として、**投稿時刻の精度問題も消える**。GitHub Actions の cron は数分〜数十分
遅れるが、この構成ではジョブは「7時ごろに起きて 9:30 の予約を積む」だけなので、
多少遅れても配信時刻は 9:30 のままになる。

LLM も使わないので、**ランニングコストはゼロ**。

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
| Secret | `BUFFER_API_KEY` | ○ |
| Secret | `QIITA_TOKEN` | 任意（無認証だと 60回/時） |
| Variable | `BUFFER_CHANNEL_ID` | 任意（指定すると解決用の2リクエストを省略） |

`BUFFER_CHANNEL_ID` は次のコマンドで確認できる：

```bash
BUFFER_API_KEY=... python src/main.py --channels
```

### 3. トピックを書く

`config.yaml` の `topics` を自分の興味に置き換える。

- `qiita_tags` … Qiita のタグ。正確な表記は https://qiita.com/tags で確認できる
- `qiita_queries` … Qiita の検索構文をそのまま書ける（`stocks:>=50` など）
- `feeds` … 信頼できる媒体の RSS

Qiita は LGTM 数が取れるので話題性の指標に、RSS は一次情報の網羅に使う、という役割分担。

Qiita API は**認証なしでも動く**が、IP あたり 60回/時の制限がある。トークンを使うと
1000回/時になる。必要なら https://qiita.com/settings/applications で発行し
（`read_qiita` 権限のみでよい）、`QIITA_TOKEN` として渡す。タグ数が少なければ無認証で足りる。

### 4. 試す

```bash
pip install -r requirements.txt
python src/main.py --dry-run
```

`--dry-run` は外部に何も送らず、選ばれた記事・投稿文・予約先の時刻だけを出す。
**API キーは一切不要**なので、まずこれでトピックと選定ルールを詰めるとよい。

### 5. 有効化

workflow を push すると cron が動きはじめる。最初は Actions タブから
`workflow_dispatch` → dry_run = true で手動実行して確認する。

## 動作の詳細

- **予約時刻**: `config.yaml` の `schedule_at`（既定 09:30 JST）。実行時点でその時刻を
  過ぎていれば自動的に翌日ぶんとして予約される。`posts_per_run` が2以上なら 30 分ずつずらす。
- **重複投稿の防止**: `state/posted.json` に投稿済み URL のハッシュを 60 日分保存し、
  ワークフローが自動でコミットして戻す。ローカル実行と Actions 実行を混ぜると
  コンフリクトするので、どちらかに寄せるのが無難。
- **選定ロジック**: スコアは `(1 + log(LGTM数)) × 新しさの減衰 × 媒体の重み`。
  対数を使うのは 100 LGTM と 1000 LGTM の差を圧縮するため。減衰の半減期は既定 12 時間。
  RSS フィード由来の記事は LGTM を持たないので `feed_base_score` を代わりに使う
  （0 にすると Qiita 記事しか選ばれなくなる）。
  同じ話題を別媒体が報じている場合はタイトルの類似度で1本に畳む。
  `posts_per_run` が2以上なら、なるべく別トピックから選ぶ。
- **除外**: PR・広告・まとめ・ランキング系はタイトルの正規表現で落とす。
  取りこぼしがあれば `selection.exclude_patterns` に追加する。
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

## チューニングのしかた

`--dry-run` を何度か回して、選ばれ方に不満があれば `config.yaml` の `selection` を調整する。

| 症状 | 触るところ |
|---|---|
| 古い記事ばかり選ばれる | `recency_half_life_hours` を小さく（例 6） |
| 話題性より媒体を重視したい | `source_weights` に信頼媒体を追加、値を上げる |
| 質の低い記事が混じる | `min_likes` を 10〜20 に上げる |
| Qiita ばかり選ばれる | `feed_base_score` を上げる（既定 8） |
| RSS ばかり選ばれる | `feed_base_score` を下げる |
| 特定の連載やコーナーを弾きたい | `exclude_patterns` に正規表現を追加 |
| 同じ話題が続けて出る | 収集側の `lookback_hours` を短くする |

## 拡張のアイデア

- Buffer は Bluesky / Mastodon / LinkedIn も同じ `createPost` で扱えるので、
  `channelId` を増やすだけでクロス投稿にできる
- 要約が欲しくなったら `selector.py` の `build_text()` だけを LLM 呼び出しに差し替える。
  選定ロジックはそのまま使えるので、LLM には「選ぶ」ではなく「書く」だけをやらせればよい
- 運用初期は Buffer の下書き（`createPost` の draft モード）に積んで、目視確認してから
  手で公開する、という半自動運用にするとミスが表に出ない
