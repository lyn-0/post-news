"""記事候補の収集。RSS フィード + Qiita API。"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import feedparser
import requests

UA = "news-tweet-bot/1.0 (+https://github.com/)"
JST = timezone(timedelta(hours=9))


@dataclass
class Article:
    title: str
    url: str
    source: str
    topic: str
    published: datetime
    summary: str = ""
    hot_score: int = 0          # LGTM数など。エンゲージメント指標が無ければ 0
    metric: str = ""            # 指標の名前（"LGTM" など）。空なら指標なし
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """重複判定用キー。URLのクエリを落として正規化。"""
        p = urllib.parse.urlsplit(self.url)
        norm = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]

    def to_prompt_dict(self) -> dict:
        return {
            "id": self.key,
            "topic": self.topic,
            "title": self.title,
            "source": self.source,
            "published": self.published.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
            "score_metric": f"{self.metric}{self.hot_score}" if self.metric else "-",
            "url": self.url,
            "excerpt": self.summary[:400],
        }


def _parse_time(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _strip(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _entry_categories(entry) -> list[str]:
    """RSS の <category> / Atom の <category term>。GIGAZINE のような
    全カテゴリ混在フィードを絞り込むのに使う。"""
    return [t.get("term", "") for t in (getattr(entry, "tags", None) or []) if t.get("term")]


def fetch_feed(
    url: str,
    topic: str,
    since: datetime,
    include_categories: list[str] | None = None,
    exclude_categories: list[str] | None = None,
) -> list[Article]:
    """通常の RSS / Atom フィードから記事を取得。

    include_categories を指定すると、そのカテゴリを持つ記事だけを残す。
    GIGAZINE のように1本のフィードに食・アニメ・IT が混在する媒体で有効。
    """
    try:
        res = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        res.raise_for_status()
        parsed = feedparser.parse(res.content)
    except Exception as e:  # フィード1本のこけで全体を落とさない
        print(f"  [warn] feed failed: {url} ({e})")
        return []

    source = parsed.feed.get("title", urllib.parse.urlsplit(url).netloc)
    out = []
    for e in parsed.entries:
        pub = _parse_time(e)
        if pub < since:
            continue
        link = e.get("link")
        if not link:
            continue

        cats = _entry_categories(e)
        if include_categories and not any(c in include_categories for c in cats):
            continue
        if exclude_categories and any(c in exclude_categories for c in cats):
            continue

        out.append(
            Article(
                title=_strip(e.get("title", "")),
                url=link,
                source=source,
                topic=topic,
                published=pub,
                summary=_strip(e.get("summary", "")),
                extra={"categories": cats},
            )
        )
    return out


def normalize_feed(entry) -> dict:
    """feeds: は文字列でも dict でも書けるようにする。"""
    if isinstance(entry, str):
        return {"url": entry}
    return entry


def fetch_qiita(
    query: str,
    topic: str,
    since: datetime,
    token: str | None = None,
    per_page: int = 100,
    min_likes: int = 0,
) -> list[Article]:
    """Qiita API v2 の記事一覧。LGTM数が「ホットさ」の指標になる。

    認証なしでも叩けるが IP あたり 60回/時。トークンを渡すと 1000回/時。
    query には Qiita の検索構文がそのまま使える（tag:React, created:>=... など）。
    """
    # created 絞り込みは日付単位なので、時刻の端数は切り捨てて広めに取る
    since_date = since.astimezone(JST).strftime("%Y-%m-%d")
    full_query = f"{query} created:>={since_date}"
    if min_likes > 0:
        # API側で絞る。取得件数を節約でき、per_page の上限に押し出されにくくなる
        full_query += f" likes_count:>={min_likes}"

    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        res = requests.get(
            "https://qiita.com/api/v2/items",
            params={"page": 1, "per_page": per_page, "query": full_query},
            headers=headers,
            timeout=20,
        )
        if res.status_code == 429:
            print(f"  [warn] Qiita レート制限（60回/時）。QIITA_TOKEN を設定すると緩和されます")
            return []
        res.raise_for_status()
        items = res.json()
    except Exception as e:
        print(f"  [warn] qiita failed: {query} ({e})")
        return []

    remaining = res.headers.get("Rate-Remaining")
    if remaining is not None and int(remaining) < 10:
        print(f"  [warn] Qiita API 残り {remaining} 回")

    out = []
    for it in items:
        try:
            pub = datetime.fromisoformat(it["created_at"])
        except (KeyError, ValueError):
            continue
        if pub < since:
            continue
        out.append(
            Article(
                title=it.get("title", "").strip(),
                url=it.get("url", ""),
                source=f"Qiita/@{it.get('user', {}).get('id', '?')}",
                topic=topic,
                published=pub,
                summary=_strip(it.get("body", ""))[:400],
                hot_score=int(it.get("likes_count", 0)),
                metric="LGTM",
                extra={
                    "via": "qiita",
                    "query": query,
                    "tags": [t.get("name") for t in it.get("tags", [])],
                },
            )
        )
    return out


def collect(config: dict) -> list[Article]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=config.get("lookback_hours", 30))

    qiita_cfg = config.get("qiita", {}) or {}
    token = os.environ.get(qiita_cfg.get("token_env", "QIITA_TOKEN")) or None
    qiita_min_likes = int(qiita_cfg.get("min_likes", 0))
    # LGTM が積み上がるには時間がかかるので、Qiita は RSS より長い窓で見る
    qiita_since = now - timedelta(days=float(qiita_cfg.get("lookback_days", 7)))

    seen: dict[str, Article] = {}

    if qiita_cfg.get("enabled"):
        cond = f"LGTM{qiita_min_likes}以上 / 過去{qiita_cfg.get('lookback_days', 7)}日"
        auth = "トークンあり" if token else "認証なし(60回/時)"
        print(f"[collect] Qiita: {cond} / {auth}")

    for topic in config.get("topics", []):
        name = topic["name"]
        print(f"[collect] {name}")

        for raw in topic.get("feeds") or []:
            f = normalize_feed(raw)
            for art in fetch_feed(
                f["url"], name, since,
                f.get("include_categories"), f.get("exclude_categories"),
            ):
                seen.setdefault(art.key, art)

        if qiita_cfg.get("enabled"):
            queries = [f"tag:{t}" for t in topic.get("qiita_tags") or []]
            queries += list(topic.get("qiita_queries") or [])
            for q in queries:
                for art in fetch_qiita(
                    q, name, qiita_since, token, min_likes=qiita_min_likes
                ):
                    prev = seen.get(art.key)
                    if prev is None or art.hot_score > prev.hot_score:
                        seen[art.key] = art
                time.sleep(0.3)

    articles = list(seen.values())
    articles.sort(key=lambda a: (a.hot_score, a.published), reverse=True)
    print(f"[collect] {len(articles)} 件の候補")
    return articles
