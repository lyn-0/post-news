"""X API v2 に直接投稿する場合のモジュール（従量課金あり）。

Buffer 経由（poster_buffer.py）を使う場合、このファイルからは weighted_length と
build_tweet だけを利用する。post() を呼ばない限り requests-oauthlib は不要。
"""

from __future__ import annotations

import os
import re
import unicodedata

import requests

ENDPOINT = "https://api.x.com/2/tweets"
URL_RE = re.compile(r"https?://\S+")


def weighted_length(text: str) -> int:
    """Xの文字数カウント（280が上限）。
    ラテン文字などは1、CJKや全角は2。URLは実長に関わらず一律23。"""
    stripped = URL_RE.sub("", text)
    n_urls = len(URL_RE.findall(text))
    total = n_urls * 23
    for ch in stripped:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def build_tweet(text: str, url: str | None) -> str:
    return f"{text}\n{url}" if url else text


def post(text: str, dry_run: bool = False) -> dict:
    w = weighted_length(text)
    if w > 280:
        raise ValueError(f"文字数超過: {w}/280\n{text}")

    if dry_run:
        print(f"--- DRY RUN ({w}/280) ---\n{text}\n-------------------------")
        return {"dry_run": True}

    from requests_oauthlib import OAuth1  # Buffer 経由なら不要なので遅延import

    auth = OAuth1(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    res = requests.post(ENDPOINT, json={"text": text}, auth=auth, timeout=30)
    if res.status_code >= 400:
        raise RuntimeError(f"X API {res.status_code}: {res.text}")
    return res.json()
