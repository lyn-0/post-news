"""LLM を使わずに、スコアリングで投稿する記事を選ぶ。

「ホットさ」の主指標は Qiita の LGTM 数。そこに新しさと媒体の重みを掛ける。
RSS フィード由来の記事はエンゲージメント指標を持たないので、feed_base_score で下駄を履かせる。
投稿文は記事タイトルをそのまま使う（技術ニュースはタイトルが結論になっていることが多い）。
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

from collectors import Article
from poster import weighted_length

# タイトルに含まれていたら除外するパターン
DEFAULT_EXCLUDE = [
    r"\[?PR\]?[】\]]",
    r"【PR】",
    r"広告",
    r"スポンサー",
    r"^\s*まとめ",
    r"人気記事",
    r"週間ランキング",
    r"アフィリエイト",
]

# タイトル末尾の媒体名（" - ITmedia NEWS" など）を落とす
TRAILING_SOURCE = re.compile(r"\s*[|｜\-–—]\s*[^|｜\-–—]{2,30}\s*$")

# 区切りとして自然な位置
BREAK_CHARS = "。．.！？!?、，,」』）)】]・ "


def normalize_title(t: str) -> str:
    t = unicodedata.normalize("NFKC", t).lower()
    return re.sub(r"[\s\W_]+", "", t)


def is_excluded(title: str, patterns: list[str]) -> bool:
    return any(re.search(p, title) for p in patterns)


def source_weight(article: Article, weights: dict[str, float]) -> float:
    hay = f"{article.url} {article.source}".lower()
    for key, w in weights.items():
        if key.lower() in hay:
            return float(w)
    return 1.0


def hotness(article: Article, cfg: dict) -> float:
    """エンゲージメント指標を対数で圧縮する（10と100の差ほど、100と1000の差は効かせない）。

    RSS 由来の記事は LGTM 相当の数値を持たないため、feed_base_score を代わりに使う。
    これがないと Qiita 記事が常に勝ってしまい、Publickey などが一度も選ばれなくなる。
    """
    if article.metric:
        raw = float(article.hot_score)
    else:
        raw = float(cfg.get("feed_base_score", 8))
    return 1.0 + math.log1p(raw)


def half_life_for(article: Article, cfg: dict) -> float:
    """減衰の半減期。既定では「その記事を集めた収集ウィンドウの1/4」を使う。

    収集元ごとに時間の流れが違うのが理由。ニュースRSSは30時間の窓で集めるので
    数時間で古くなるが、Zenn の「♥100以上」は60日の窓なので数日前でも十分新しい。
    共通の固定値を使うと必ずどちらかが一方的に負けるため、窓に対する相対で測る。
    recency_half_life_hours を設定すれば全ソース共通の固定値に上書きできる。
    """
    override = cfg.get("recency_half_life_hours")
    if override:
        return float(override)
    return max(article.window_hours / 4, 1.0)


def score(article: Article, cfg: dict, now: datetime) -> float:
    """ホットさ × 新しさ × 媒体重み。"""
    age_h = max((now - article.published).total_seconds() / 3600, 0.0)
    recency = 0.5 ** (age_h / half_life_for(article, cfg))
    return hotness(article, cfg) * recency * source_weight(
        article, cfg.get("source_weights", {}) or {}
    )


def dedupe_similar(articles: list[Article], threshold: float = 0.72) -> list[Article]:
    """同じ話題を別媒体が報じているケースを1本に畳む。先に来た（＝高スコア）方を残す。"""
    kept: list[Article] = []
    norms: list[str] = []
    for a in articles:
        n = normalize_title(a.title)
        if any(SequenceMatcher(None, n, m).ratio() >= threshold for m in norms):
            continue
        kept.append(a)
        norms.append(n)
    return kept


def clean_title(title: str, strip_source: bool) -> str:
    t = title.strip()
    if strip_source:
        stripped = TRAILING_SOURCE.sub("", t)
        # 削りすぎ防止。元の6割を切るなら戻す
        if weighted_length(stripped) >= weighted_length(t) * 0.6:
            t = stripped
    return t.strip()


def fit(text: str, budget: int) -> str:
    """weighted length が budget に収まるよう、区切りのよい位置で切る。"""
    if weighted_length(text) <= budget:
        return text
    out = ""
    for ch in text:
        if weighted_length(out + ch) > budget - 2:  # 省略記号ぶん
            break
        out += ch
    # 直近の区切り文字まで戻す（戻しすぎない範囲で）
    for i in range(len(out) - 1, max(len(out) - 12, 0), -1):
        if out[i] in BREAK_CHARS:
            out = out[: i + 1]
            break
    return out.rstrip(BREAK_CHARS) + "…"


def build_text(article: Article, cfg: dict, budget: int) -> str:
    title = clean_title(article.title, cfg.get("strip_source_from_title", True))

    tag = (cfg.get("hashtags_by_topic", {}) or {}).get(article.topic, "")
    suffix = f"\n{tag}" if tag else ""

    body_budget = budget - weighted_length(suffix)
    return fit(title, body_budget) + suffix


def pick(
    articles: list[Article],
    count: int,
    cfg: dict,
    include_link: bool,
) -> list[dict]:
    """スコアリングで投稿する記事を選ぶ。"""
    now = datetime.now(timezone.utc)
    patterns = DEFAULT_EXCLUDE + list(cfg.get("exclude_patterns", []) or [])
    min_len = int(cfg.get("min_title_length", 8))

    pool = [
        a
        for a in articles
        if not is_excluded(a.title, patterns)
        and len(a.title) >= min_len
    ]
    dropped = len(articles) - len(pool)
    if dropped:
        print(f"[select] 除外 {dropped} 件（PR/広告/タイトル短すぎ）")
    if not pool:
        return []

    pool.sort(key=lambda a: score(a, cfg, now), reverse=True)
    pool = dedupe_similar(pool)

    # 複数本選ぶときはトピックを散らす
    picked: list[Article] = []
    used_topics: set[str] = set()
    for a in pool:
        if len(picked) >= count:
            break
        if a.topic not in used_topics:
            picked.append(a)
            used_topics.add(a.topic)
    for a in pool:  # 埋まらなければトピック重複を許す
        if len(picked) >= count:
            break
        if a not in picked:
            picked.append(a)

    # URL は t.co で23文字固定 ＝ 全角11.5字ぶん
    budget = 118 if include_link else 138

    results = []
    for a in picked:
        age_h = int((now - a.published).total_seconds() / 3600)
        results.append(
            {
                "article": a,
                "text": build_text(a, cfg, budget),
                "why": (f"{a.metric}{a.hot_score}" if a.metric else a.source)
                       + f" / {age_h}時間前 / score {score(a, cfg, now):.2f}",
            }
        )
    return results
