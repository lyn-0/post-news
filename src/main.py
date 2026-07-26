"""興味トピックのホットな記事を集め、Claude に要約させて X に投稿する。

  python src/main.py --dry-run    # 投稿せず内容だけ確認（ANTHROPIC_API_KEY のみ必要）
  python src/main.py              # 実行
  python src/main.py --channels   # Buffer に繋がっているチャンネル一覧を表示
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import poster_buffer
from collectors import collect
from poster import build_tweet, weighted_length
from summarize import pick_and_write

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "posted.json"
JST = timezone(timedelta(hours=9))
KEEP_DAYS = 60


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"posted": {}}


def save_state(state: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    state["posted"] = {k: v for k, v in state["posted"].items() if v.get("at", "") >= cutoff}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def show_channels() -> int:
    """接続済みチャンネルの一覧。channelId を確認したいときに使う。"""
    client = poster_buffer.BufferClient()
    org = client.organization_id()
    print(f"organization: {org}\n")
    for c in client.channels(org):
        paused = " [キュー停止中]" if c.get("isQueuePaused") else ""
        print(f"  {c['service']:12} {c['displayName']:24} id={c['id']}{paused}")
    return 0


def slot_for(schedule_at: str, index: int) -> str:
    """複数本投稿するとき、30分ずつずらしたスロットを返す。"""
    hh, mm = (int(x) for x in schedule_at.split(":"))
    total = (hh * 60 + mm + 30 * index) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--channels", action="store_true", help="Bufferのチャンネル一覧を表示して終了")
    args = ap.parse_args()

    if args.channels:
        return show_channels()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    include_link = config.get("include_link", True)
    schedule_at = str(config.get("schedule_at", "09:30"))
    state = load_state()

    # 1. 収集
    articles = collect(config)

    # 2. 既出を除外
    articles = [a for a in articles if a.key not in state["posted"]]
    if not articles:
        print("[skip] 新しい候補がありません")
        return 0
    print(f"[filter] 未投稿の候補 {len(articles)} 件")

    # 3. Claude に選定と本文生成をさせる
    picks = pick_and_write(
        articles,
        count=config.get("posts_per_run", 1),
        model=config.get("model", "claude-sonnet-5"),
        tone=config.get("tone", ""),
        include_link=include_link,
    )
    if not picks:
        print("[skip] 投稿に値する記事が選ばれませんでした")
        return 0

    for p in picks:
        art = p["article"]
        p["tweet"] = build_tweet(p["text"], art.url if include_link else None)
        print(f"\n[pick] {art.title}\n       理由: {p['why']} / はてブ {art.hot_score}"
              f"\n       {weighted_length(p['tweet'])}/280")

    # 4. Buffer に予約を積む
    for i, p in enumerate(picks):
        try:
            poster_buffer.publish(p["tweet"], slot_for(schedule_at, i), dry_run=args.dry_run)
        except Exception as e:
            print(f"[error] 予約失敗: {e}", file=sys.stderr)
            return 1
        if not args.dry_run:
            art = p["article"]
            state["posted"][art.key] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "title": art.title,
                "url": art.url,
            }
        time.sleep(1)

    if not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
