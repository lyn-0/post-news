"""候補記事から「今日投稿すべき記事」を選び、投稿文を生成する。"""

from __future__ import annotations

import json
import os

import anthropic

from collectors import Article

SYSTEM = """あなたは技術系ニュースをXに投稿する編集者です。
渡された候補記事から、指定された本数だけ「今日いちばん読む価値がある記事」を選び、
それぞれの投稿文を書きます。

選定基準（重要な順）:
1. 新規性 — 昨日今日はじめて分かった事実か。焼き直し・まとめ記事・広告記事は選ばない
2. 影響範囲 — 読者（Web開発者）の仕事や判断が実際に変わりうるか
3. 信頼性 — 一次情報またはそれに近い媒体か。憶測ベースの記事は避ける
4. トピックの分散 — 複数本選ぶ場合、同じトピックに偏らせない

投稿文の制約:
- 日本語。全角換算で {limit} 文字以内（厳守）。URLは本文に含めない（システムが末尾に付けます）
- 1行目に結論。何が起きたのかを最初の30文字で分からせる
- 記事に書かれていない事実を足さない。数値は記事にあるものだけ使う
- 煽り表現（「衝撃」「ヤバい」「知らないと損」等）は使わない

出力は次のJSONのみ。前置き・コードフェンス・説明は一切書かないこと。
{{"picks": [{{"id": "候補のid", "text": "投稿本文", "why": "選んだ理由(20字程度、ログ用)"}}]}}"""


def pick_and_write(
    articles: list[Article],
    count: int,
    model: str,
    tone: str,
    include_link: bool,
    max_candidates: int = 40,
) -> list[dict]:
    if not articles:
        return []

    # URLはt.co短縮で常に23文字＝全角11.5文字相当。余裕をみて確保する
    limit = 118 if include_link else 138

    candidates = [a.to_prompt_dict() for a in articles[:max_candidates]]
    user_msg = (
        f"# 文体の指示\n{tone}\n\n"
        f"# 選ぶ本数\n{count}\n\n"
        f"# 候補記事\n{json.dumps(candidates, ensure_ascii=False, indent=1)}"
    )

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    res = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM.format(limit=limit),
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = "".join(b.text for b in res.content if b.type == "text").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        picks = json.loads(raw)["picks"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise RuntimeError(f"モデル出力のJSONパースに失敗: {e}\n---\n{raw[:800]}") from e

    by_id = {a.key: a for a in articles}
    out = []
    for p in picks[:count]:
        art = by_id.get(p.get("id"))
        if art is None:
            print(f"  [warn] 未知のid をスキップ: {p.get('id')}")
            continue
        out.append({"article": art, "text": p["text"].strip(), "why": p.get("why", "")})
    return out
