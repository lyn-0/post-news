"""記事候補の収集。RSS フィード + はてなブックマーク検索。"""

from __future__ import annotations

import hashlib
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
    hot_score: int = 0          # はてブ数など（無ければ0）
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
            "hatena_users": self.hot_score,
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


def fetch_feed(url: str, topic: str, since: datetime) -> list[Article]:
    """通常の RSS / Atom フィードから記事を取得。"""
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
        out.append(
            Article(
                title=_strip(e.get("title", "")),
                url=link,
                source=source,
                topic=topic,
                published=pub,
                summary=_strip(e.get("summary", "")),
            )
        )
    return out


def fetch_hatena(keyword: str, topic: str, since: datetime, min_users: int) -> list[Article]:
    """はてなブックマークの検索RSS。ブクマ数が「ホットさ」の代理指標になる。"""
    q = urllib.parse.quote(keyword)
    url = (
        f"https://b.hatena.ne.jp/search/text"
        f"?q={q}&mode=rss&sort=recent&users={max(min_users, 1)}&safe=on"
    )
    try:
        res = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        res.raise_for_status()
        parsed = feedparser.parse(res.content)
    except Exception as e:
        print(f"  [warn] hatena failed: {keyword} ({e})")
        return []

    out = []
    for e in parsed.entries:
        pub = _parse_time(e)
        if pub < since:
            continue
        link = e.get("link")
        if not link:
            continue
        try:
            users = int(e.get("hatena_bookmarkcount", 0))
        except (TypeError, ValueError):
            users = 0
        if users < min_users:
            continue
        out.append(
            Article(
                title=_strip(e.get("title", "")),
                url=link,
                source=urllib.parse.urlsplit(link).netloc,
                topic=topic,
                published=pub,
                summary=_strip(e.get("description", "")),
                hot_score=users,
                extra={"via": "hatena", "keyword": keyword},
            )
        )
    return out


def collect(config: dict) -> list[Article]:
    since = datetime.now(timezone.utc) - timedelta(hours=config.get("lookback_hours", 30))
    hatena_cfg = config.get("hatena", {}) or {}
    seen: dict[str, Article] = {}

    for topic in config.get("topics", []):
        name = topic["name"]
        print(f"[collect] {name}")

        for feed_url in topic.get("feeds") or []:
            for art in fetch_feed(feed_url, name, since):
                seen.setdefault(art.key, art)

        if hatena_cfg.get("enabled"):
            for kw in topic.get("keywords") or []:
                for art in fetch_hatena(kw, name, since, hatena_cfg.get("min_users", 5)):
                    # 同じ記事が両方から来たらブクマ数を持っている方を優先
                    prev = seen.get(art.key)
                    if prev is None or art.hot_score > prev.hot_score:
                        seen[art.key] = art
                time.sleep(0.5)  # はてな側への配慮

    articles = list(seen.values())
    articles.sort(key=lambda a: (a.hot_score, a.published), reverse=True)
    print(f"[collect] {len(articles)} 件の候補")
    return articles
