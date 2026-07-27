"""興味トピックのホットな記事を集め、Claude に要約させて X に投稿する。

  python src/main.py --dry-run    # 投稿せず内容だけ確認（APIキー不要）
  python src/main.py              # 実行（9:30に予約）
  python src/main.py --now        # 即時投稿（テスト用）
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
from selector import pick

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
    # cycle_index は posted と同じ階層に置く（掃除対象にしない）
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def show_channels(verbose: bool = False) -> int:
    """接続済みチャンネルの一覧と、直近の投稿の配信結果を表示する。"""
    client = poster_buffer.BufferClient()
    orgs = client.organizations()
    print(f"organization: {len(orgs)} 件")
    for o in orgs:
        print(f"  - {o['name']} (id={o['id']})")

    chans = client.all_channels()
    if not chans:
        print("\nチャンネルが1件もありません。"
              "Buffer の Publish 画面で X が接続済みか、"
              "APIキーを発行したアカウントと同じ組織かを確認してください。")
        return 1

    print(f"\nchannels: {len(chans)} 件")
    for c in chans:
        name = c.get("displayName") or c.get("name") or "(名前なし)"
        flags = []
        if c.get("isQueuePaused"):
            flags.append("キュー停止中")
        if c.get("isDisconnected"):
            flags.append("接続切れ→再認証が必要")
        if c.get("isLocked"):
            flags.append("ロック中→プラン上限")
        mark = ("  ⚠ " + " / ".join(flags)) if flags else "  OK"
        print(f"  {str(c.get('service')):10} {name:22} id={c['id']}{mark}")

    if not verbose:
        return 0

    # 直近の投稿がどうなったかを見る
    x = [c for c in chans if (c.get("service") or "").lower() in poster_buffer.X_SERVICES]
    for c in x:
        print(f"\n直近の投稿 ({c.get('displayName') or c['id']}):")
        try:
            posts = client.recent_posts(c["organizationId"], c["id"], limit=10)
        except Exception as e:
            print(f"  取得できませんでした: {e}")
            continue
        if not posts:
            print("  1件もありません。Buffer に投稿が届いていません")
            continue
        for n in posts:
            line = f"  [{n.get('status')}] {n.get('shareMode')} due={n.get('dueAt')}"
            if n.get("sentAt"):
                line += f" sent={n['sentAt']}"
            print(line)
            print(f"      {(n.get('text') or '')[:50]}")
            if n.get("externalLink"):
                print(f"      -> {n['externalLink']}")
            if n.get("error"):
                print(f"      ✗ {n['error'].get('message')}")
    return 0


def check_feeds(config: dict) -> int:
    """設定した RSS フィードが実際に取れるか確認する。URL の打ち間違い検出用。"""
    from collectors import fetch_feed, normalize_feed

    since = datetime.now(timezone.utc) - timedelta(days=30)
    total, dead = 0, 0
    for topic in config.get("topics", []):
        print(f"\n[{topic['name']}]")
        urls = topic.get("feeds") or []
        if not urls:
            print("  フィードが設定されていません")
            continue
        for raw in urls:
            f = normalize_feed(raw)
            url = f["url"]
            arts = fetch_feed(
                url, topic["name"], since,
                f.get("include_categories"), f.get("exclude_categories"),
            )
            total += len(arts)
            if not arts:
                dead += 1
                print(f"  ✗ {url}\n      過去30日で0件。URLか配信状況を確認してください")
            else:
                newest = max(a.published for a in arts)
                age_h = int((datetime.now(timezone.utc) - newest).total_seconds() / 3600)
                print(f"  ✓ {len(arts):>3}件 (最新 {age_h}時間前)  {url}")

    print(f"\n合計 {total} 件 / 取得できなかったフィード {dead} 本")
    if dead:
        print("取得できないフィードがあっても実行時は警告を出して読み飛ばします")
    return 0


def simulate(config: dict, days: int) -> int:
    """投稿せずに、この先 N 回ぶんの選ばれ方を並べて見せる。
    dry-run は状態が進まないので、比率の確認にはこちらを使う。"""
    from copy import copy

    state = load_state()
    articles = collect(config)
    articles = [a for a in articles if a.key not in state["posted"]]
    sel = dict(config.get("selection", {}) or {}, show_ranking=False)
    include_link = config.get("include_link", True)
    idx = int(state.get("cycle_index", 0))

    print(f"\n=== {days}回ぶんのシミュレーション（実際には投稿しません）===")
    used: set[str] = set()
    counts: dict[str, int] = {}
    for n in range(1, days + 1):
        pool = [a for a in articles if a.key not in used]
        got = pick(pool, 1, sel, include_link, cycle_index=idx)
        if not got:
            print(f"  {n:>2}回目: 候補切れ")
            break
        art = got[0]["article"]
        used.add(art.key)
        counts[art.kind] = counts.get(art.kind, 0) + 1
        idx += 1
        metric = f"{art.metric}{art.hot_score}" if art.metric else "-"
        print(f"  {n:>2}回目: [{art.kind:5}] {metric:>7}  {art.title[:44]}")

    total = sum(counts.values()) or 1
    print("\n内訳: " + " / ".join(
        f"{k} {v}回 ({v / total:.0%})" for k, v in sorted(counts.items())))
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
    ap.add_argument("--now", action="store_true",
                    help="9:30の予約ではなく即時投稿する（テスト実行用）")
    ap.add_argument("--channels", action="store_true", help="Bufferのチャンネル一覧を表示して終了")
    ap.add_argument("--diagnose", action="store_true",
                    help="チャンネル状態＋直近の投稿の配信結果を表示して終了")
    ap.add_argument("--check-feeds", action="store_true",
                    help="設定した RSS フィードが取得できるか確認して終了")
    ap.add_argument("--simulate", type=int, metavar="N",
                    help="この先N回ぶんの選ばれ方を投稿せずに確認する")
    args = ap.parse_args()

    if args.channels or args.diagnose:
        return show_channels(verbose=args.diagnose)

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    if args.check_feeds:
        return check_feeds(config)

    if args.simulate:
        return simulate(config, args.simulate)
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

    # 3. スコアリングで選定し、投稿文を組み立てる
    picks = pick(
        articles,
        count=config.get("posts_per_run", 1),
        cfg=config.get("selection", {}) or {},
        include_link=include_link,
        cycle_index=int(state.get("cycle_index", 0)),
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
            poster_buffer.publish(
                p["tweet"], slot_for(schedule_at, i),
                dry_run=args.dry_run, now=args.now,
            )
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
            state["cycle_index"] = int(state.get("cycle_index", 0)) + 1
        time.sleep(1)

    if not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
