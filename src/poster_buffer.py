"""Buffer 経由での投稿。

自分で X API を契約せず、Buffer に予約投稿を積む。X への配信は Buffer が行う。
`customScheduled` + `dueAt` を使うので、スクリプトの実行時刻と投稿時刻を分離できる
（＝ GitHub Actions の cron 遅延を気にしなくてよくなる）。

参照: https://developers.buffer.com/guides/posts-and-scheduling.html
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from poster import weighted_length  # X の文字数計算は共通で使う

ENDPOINT = "https://api.buffer.com"
JST = timezone(timedelta(hours=9))
X_SERVICES = {"twitter", "x"}


def _lit(value: str) -> str:
    """Python文字列をGraphQLの文字列リテラルに変換する。

    createPost の input 型名が将来変わってもいいように、GraphQL変数ではなく
    リテラル埋め込みにしている。JSONの文字列エスケープはGraphQLと互換なので
    json.dumps をそのまま使える（引用符・改行・Unicodeすべて安全に処理される）。
    """
    return json.dumps(value, ensure_ascii=False)


class BufferClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["BUFFER_API_KEY"]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def gql(self, query: str) -> dict:
        res = self.session.post(ENDPOINT, json={"query": query}, timeout=30)
        if res.status_code == 401:
            raise RuntimeError("Buffer APIキーが無効です（401）")
        if res.status_code >= 400:
            raise RuntimeError(f"Buffer API {res.status_code}: {res.text[:500]}")
        body = res.json()
        if body.get("errors"):
            raise RuntimeError(f"Buffer GraphQL error: {body['errors']}")
        return body["data"]

    # ---- チャンネル解決 -------------------------------------------------

    def organizations(self) -> list[dict]:
        data = self.gql("query { account { organizations { id name } } }")
        orgs = data["account"]["organizations"]
        if not orgs:
            raise RuntimeError("Buffer に organization がありません")
        return orgs

    def organization_id(self) -> str:
        return self.organizations()[0]["id"]

    def all_channels(self) -> list[dict]:
        """全 organization を横断してチャンネルを集める。
        組織が複数ある場合、X が先頭以外に紐づいていることがあるため。"""
        out = []
        for org in self.organizations():
            for c in self.channels(org["id"]):
                out.append({**c, "organizationId": org["id"], "organizationName": org["name"]})
        return out

    def channels(self, org_id: str) -> list[dict]:
        data = self.gql(
            f"query {{ channels(input: {{ organizationId: {_lit(org_id)} }}) "
            f"{{ id name displayName service isQueuePaused }} }}"
        )
        return data["channels"]

    def resolve_x_channel(self) -> dict:
        """X チャンネルを自動で見つける。BUFFER_CHANNEL_ID があればAPI呼び出しを省略。"""
        forced = os.environ.get("BUFFER_CHANNEL_ID")
        if forced:
            return {"id": forced, "displayName": "(env指定)", "isQueuePaused": False}

        chans = self.all_channels()
        matched = [c for c in chans if (c.get("service") or "").lower() in X_SERVICES]
        if not matched:
            names = ", ".join(
                f"{c.get('displayName') or c.get('name') or '?'}({c.get('service')})"
                for c in chans
            ) or "なし"
            raise RuntimeError(f"X チャンネルが見つかりません。接続済み: {names}")
        if len(matched) > 1:
            print(f"  [warn] X チャンネルが複数あります。先頭を使用: "
                  f"{[c['displayName'] for c in matched]}")
        return matched[0]

    # ---- 投稿 -----------------------------------------------------------

    def create_scheduled_post(self, text: str, channel_id: str, due_at: datetime) -> dict:
        due_utc = due_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        mutation = f"""
mutation {{
  createPost(input: {{
    text: {_lit(text)}
    channelId: {_lit(channel_id)}
    schedulingType: automatic
    mode: customScheduled
    dueAt: {_lit(due_utc)}
  }}) {{
    ... on PostActionSuccess {{ post {{ id dueAt status }} }}
    ... on MutationError {{ message }}
  }}
}}"""
        result = self.gql(mutation)["createPost"]
        if "message" in result:
            raise RuntimeError(f"Buffer が投稿を拒否しました: {result['message']}")
        return result["post"]


def next_occurrence(hhmm: str, now: datetime | None = None) -> datetime:
    """JSTで次に来る指定時刻を返す。今日の分が過ぎていれば翌日。"""
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = now or datetime.now(JST)
    target = now.astimezone(JST).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def publish(text: str, schedule_at: str, dry_run: bool = False) -> dict:
    w = weighted_length(text)
    if w > 280:
        raise ValueError(f"文字数超過: {w}/280\n{text}")

    due = next_occurrence(schedule_at)

    if dry_run:
        print(f"--- DRY RUN ({w}/280) 予約先: {due:%Y-%m-%d %H:%M} JST ---\n"
              f"{text}\n--------------------------------------------------")
        return {"dry_run": True, "dueAt": due.isoformat()}

    client = BufferClient()
    channel = client.resolve_x_channel()
    if channel.get("isQueuePaused"):
        print(f"  [warn] チャンネル '{channel.get('displayName')}' のキューが停止中です。"
              f"Buffer 側で再開しないと配信されません")
    post = client.create_scheduled_post(text, channel["id"], due)
    print(f"[buffer] 予約完了 id={post['id']} dueAt={post['dueAt']}")
    return post
