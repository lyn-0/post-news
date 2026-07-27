# news-tweet-bot

興味のあるトピックのホットな記事を毎朝集め、9:30 JST に X へ投稿する。
**API キーは Buffer のものだけ**。LLM は使わず、スコアリングで記事を選ぶ。

```
RSS + Zenn ─▶ 既出除外 ─▶ スコアで選定 ─▶ Buffer に予約 ─▶ 9:30 に X へ
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

`config.yaml` の `topics` を自分の興味に置き換える。`feeds` に RSS の URL を並べるだけ。

設定したら、まず疎通を確認する:

```bash
python src/main.py --check-feeds       # フィードが取れるか確認
python src/main.py --simulate 10       # この先10回ぶんの選ばれ方を確認
```

`--simulate` は投稿せずに、収集元の比率と実際に選ばれる記事を並べて見せる。
`--dry-run` は状態（`cycle_index`）が進まないため何度実行しても同じ結果になるので、
比率の確認にはこちらを使うこと。

各フィードの取得件数と最新記事の鮮度が出る。`✗` が付いたものは URL が違うか配信が
止まっている。取れないフィードがあっても実行時は警読み飛ばすので落ちはしないが、
候補が薄くなるので直しておくとよい。

`feeds` は URL の文字列でも、カテゴリ絞り込み付きの dict でも書ける:

```yaml
feeds:
  - https://example.com/feed          # そのまま全部取る
  - url: https://gigazine.net/news/rss_2.0/
    include_categories: [AI, ソフトウェア]   # このカテゴリだけ残す
    # exclude_categories: [試食, アニメ]     # 逆に除外もできる
```

GIGAZINE のように1本のフィードに技術・食・アニメが混在する媒体では
`include_categories` が必須。RSS の `<category>` を見て判定する。

**フィードは多めに入れること。** RSS のみで運用する場合、候補は
`lookback_hours`（既定30時間）以内に配信された記事だけになる。フィードが2〜3本だと
候補がゼロになる日が出る。5〜10本を目安に。

### Zenn

Zenn の RSS にはいいね数が入っていないため、サイト内部の `/api/articles` を使っている。
`zenn.min_likes`（既定100）でいいね数の下限を、`zenn_topics` でトピックを指定する。
トピック名は `zenn.dev/topics/xxx` の `xxx` の部分。

これは Zenn が公式にドキュメント化した API ではない。予告なく変わる可能性があるので、
壊れたら `collectors.py` の `fetch_zenn()` だけ直せばよい設計にしてある。

### Qiita を併用したい場合

`qiita.enabled: true` にすると、Qiita API から LGTM 数付きで記事を集める。
コードと設定はそのまま残してあるので、切り替えるだけで戻せる。

ただし `qiita.lookback_days` を伸ばす場合は `selection.recency_half_life_hours` も
必ず一緒に伸ばすこと（目安は収集期間の 1/4）。釣り合っていないと起動時に警告が出る。

### 4. 試す

```bash
pip install -r requirements.txt
python src/main.py --dry-run
```

`--dry-run` は外部に何も送らず、選ばれた記事・投稿文・予約先の時刻だけを出す。
**API キーは一切不要**なので、まずこれでトピックと選定ルールを詰めるとよい。

### 5. 有効化

workflow を push すると cron が動きはじめる。Actions タブから手動実行するときは
`mode` を選ぶ。

| mode | 挙動 |
|---|---|
| `dry-run`（既定） | Buffer にも X にも何も送らず、選ばれた記事だけログに出す |
| `post-now` | 9:30 を待たず**今すぐ投稿する**。動作確認用 |
| `schedule` | 通常どおり 9:30 JST に予約する |

cron からの起動は常に `schedule` 相当。まず `dry-run` で選定を確認し、
納得したら `post-now` で実際に流れるところまで通す、という順番がよい。

`post-now` は本当に投稿されるので注意。実行すると `state/posted.json` にも
記録され、その記事は二度と選ばれなくなる。

## 動作の詳細

- **予約時刻**: `config.yaml` の `schedule_at`（既定 09:30 JST）。実行時点でその時刻を
  過ぎていれば自動的に翌日ぶんとして予約される。`posts_per_run` が2以上なら 30 分ずつずらす。
  `--now`（Actions では `post-now`）を付けると予約せず即時投稿になる
  （Buffer の `mode: shareNow`）。
- **重複投稿の防止**: `state/posted.json` に投稿済み URL のハッシュを 60 日分保存し、
  ワークフローが自動でコミットして戻す。ローカル実行と Actions 実行を混ぜると
  コンフリクトするので、どちらかに寄せるのが無難。
- **選定ロジック**: スコアは `(1 + log(いいね数)) × 新しさの減衰 × 媒体の重み`。
  対数を使うのは 100 LGTM と 1000 LGTM の差を圧縮するため。減衰の半減期は既定 12 時間。
  RSS フィード由来の記事は LGTM を持たないので `feed_base_score` を代わりに使う
  （0 にすると Qiita 記事しか選ばれなくなる）。
- **収集元の比率（source_cycle）**: 1回の実行で枠を1つ消化し、リストの先頭から順に回る。
  既定は `[feed, feed, feed, zenn]` で「RSS 3回 : Zenn 1回」の4日周期。
  現在位置は `state/posted.json` の `cycle_index` に記録される。

  ```yaml
  source_cycle: [feed, feed, feed, zenn]  # 4回に1回だけ Zenn
  source_cycle: [feed]                    # Zenn は候補が他に無いときだけ
  source_cycle: []                        # ローテーションせず純粋なスコア順
  ```

  これが必要な理由は、収集元によってスコアの出方が構造的に違うから。Zenn の
  「♥100以上・60日窓」の記事は2週間経ってもスコア4前後を保つが、ニュースRSSには
  エンゲージメント指標が無く時間で減衰するだけなので、単一のスコアで比べると
  必ず Zenn に偏る。`feed_base_score` をいくら上げても構造は変わらない。

  枠に該当する候補が無い日はスコア順にフォールバックする（投稿が飛ぶことはない）。
  ログに「今回の枠は feed ですが候補がないため」と出たら、RSS の候補が
  足りていないサイン。フィードを増やすか `lookback_hours` を伸ばす。
- **半減期の下限**: `min_half_life_hours`（既定24）。1日1回しか投稿しないので、
  これより短いと前夜のニュースが朝の実行時点で減衰しきってしまう。
- **半減期は収集元ごとに自動で決まる**: 既定（`recency_half_life_hours: null`）では
  「その記事を集めた収集ウィンドウの 1/4」を半減期に使う。ニュースRSSは30時間の窓なので
  半減期7.5時間、Zenn は60日の窓なので半減期15日。収集元によって時間の流れが違うため、
  共通の固定値にすると必ずどちらかが一方的に負ける。数値を入れれば全ソース共通に固定できる。
- **LGTM の下限と収集期間はセット**: `qiita.min_likes` は Qiita API 側で絞る
  （`likes_count:>=N` をクエリに付ける）。LGTM が積み上がるには数日かかるので、
  Qiita だけ `qiita.lookback_days`（既定7日）という別の窓を使う。RSS は
  `lookback_hours`（既定30時間）のまま。

- **取得件数の上限**: Qiita は1タグにつき1リクエスト・最大100件しか取らない。
  収集期間を60日にすると人気タグでは100件を超えることがあるが、API は新しい順に
  返すので「直近100件」が対象になる。減衰が効いている以上、実用上の影響は小さい。
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
| 候補がゼロになる日がある | フィードを増やす、`lookback_hours` を伸ばす |
| Zenn ばかり選ばれる | `source_cycle` の `feed` を増やす。数値調整より確実 |
| RSS が全く選ばれない | `min_half_life_hours` を上げる（既定24） |
| 選ばれ方の理由が知りたい | `show_ranking: true`（既定）でスコア内訳がログに出る |
| 同じ媒体ばかり選ばれる | `source_weights` の値を下げる／他媒体を上げる |
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
