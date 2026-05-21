"""
note の記事から Threads へ自動投稿するスクリプト
================================================
最終更新: 2026-05-21

動作概要:
  1. note の RSS から最新記事を取得
  2. 投稿済み履歴と照合し、未投稿記事 or ローテーション対象を選択
  3. 記事から Threads 向けの短い投稿文を生成（先頭フック + 引用 + 元記事URL）
  4. Threads Graph API へ投稿

実行例:
  # 通常実行（API投稿あり）
  python post_to_threads.py

  # ドライラン（投稿文生成までで止める / API は叩かない）
  python post_to_threads.py --dry-run

  # 特定記事を強制投稿
  python post_to_threads.py --url https://note.com/xelvon/n/xxxxxxxx

環境変数（.env もしくは OS 環境変数で設定）:
  THREADS_USER_ID        : Threads ユーザー ID（数値）
  THREADS_ACCESS_TOKEN   : Threads 長期アクセストークン
  NOTE_USERNAME          : note のユーザー名（例: xelvon）デフォルト xelvon
  POST_HISTORY_PATH      : 投稿履歴 JSON のパス（省略可）

依存:
  pip install feedparser requests beautifulsoup4 python-dotenv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import anthropic
    import feedparser
    import requests
    from bs4 import BeautifulSoup
    from dotenv import load_dotenv
    import googleapiclient.discovery
    from google.oauth2 import service_account
except ImportError as e:
    print(
        f"必要なライブラリが不足しています: {e}\n"
        "pip install feedparser requests beautifulsoup4 python-dotenv anthropic "
        "google-api-python-client google-auth\n"
        "を実行してください。",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------- 定数 ----------

THREADS_API_BASE = "https://graph.threads.net/v1.0"
THREADS_MAX_LEN = 500  # Threadsの本文上限
DEFAULT_NOTE_USERNAME = "xelvon"
DEFAULT_HISTORY_PATH = Path(__file__).parent / "post_history.json"

# 投稿フォーマットのバリエーション（同じ記事でも違う切り口で投げられるように）
HOOK_TEMPLATES = [
    "📝 {title}",
    "今日のnote記事より👇\n{title}",
    "ふと書いた話：{title}",
    "{title}",
    "💭 {title}",
]


# 除外したい記事タイトルのキーワードリスト
EXCLUDE_KEYWORDS = [
    "ママがお空に行く時は",  # 今回の母の日の記事
    # 今後他にもあればここに追加できます
]

# Threads プロフィールへのCTA（全通常投稿の末尾に付加）
THREADS_PROFILE_CTA = "プロフのリンクから↓\nhttps://www.threads.com/@xelvon.jp"

# 通常投稿のプロンプトスタイル（4種ローテーション）
_REGULAR_PROMPT_STYLES = [
    # A: 感情の吐露・共感フック
    """\
■ スタイル A: 感情の吐露・共感フック
- 冒頭1行で「あ、これ自分のことだ」と感じる感情的な共感フックから始める
- 自分の体験・気持ちの流れを短い文で語るストーリー形式で展開する
- 末尾は問いかけ（「あなたはどう？」など）または余韻で締める""",
    # B: 逆説・意外な気づき
    """\
■ スタイル B: 逆説・意外な気づき
- 「〜だと思っていたのに」「当たり前だと思っていたことが実は違った」という逆説から書き始める
- 読者の固定観念をひっくり返す切り口で展開する
- 末尾は「あなたはどう思う？」系の問いかけで締める""",
    # C: 場面・セリフの切り取り
    """\
■ スタイル C: 場面・セリフの切り取り
- 特定の一瞬・セリフ・情景から書き始める（「子どもに言われた一言」「ふと気づいた瞬間」など）
- 映像が浮かぶような具体的な描写で読者を引き込み、その場面が持つ意味・感情で締める
- 情景描写を大切に、詩的な余韻を残す終わり方にする""",
    # D: 自問自答・内省
    """\
■ スタイル D: 自問自答・内省
- 「なぜ自分は〜しているんだろう」「〜でいいのか、ずっと迷っている」のように自分への問いかけから始まる
- 自分の葛藤・迷い・気づきのプロセスを正直に語る
- 読者も同じ問いを持てるような言葉で締める""",
]

# バズ投稿のプロンプトスタイル（4種ローテーション）
_VIRAL_PROMPT_STYLES = [
    # A: 感情の吐露
    """\
■ スタイル A: 感情の吐露
- 思わずスマホを打ち込んだような自然な呟きのトーン
- 記事から「一番心に刺さる感情」を一つだけ深掘りして、感情が動く表現を使う（寂しい、悔しい、嬉しい、複雑など）
- 「ハッとした」は使わず、より具体的な感情語を選ぶ
- 末尾は問いかけ（「あなたはどう？」「同じ人いる？」）または余韻で締める""",
    # B: モヤモヤ・違和感
    """\
■ スタイル B: モヤモヤ・違和感
- 「なんか違う」「ずっとモヤモヤしてた」「これって私だけ？」のような小さな怒りや違和感から書き始める
- 共感を呼ぶ「言語化されてなかった感情」を言葉にする
- 賛否が出てもいい、刺さる人には深く刺さる内容にする""",
    # C: 驚き・発見
    """\
■ スタイル C: 驚き・発見
- 「え、こういうことだったの？」「今更気づいたんだけど」という驚きや発見の瞬間を切り取る
- 「知らなかったのは自分だけ？」と思わせるような身近な気づきを書く
- 「わかる」「私も！」とコメントしたくなる内容にする""",
    # D: 寂しさ・切なさ
    """\
■ スタイル D: 寂しさ・切なさ
- 子育て・家族・日常の中にある、ふとした寂しさや切なさを切り取る
- 「こんなこと言えなかったけど」という打ち明け話のトーンで書く
- 読んだ人が「自分も同じ気持ちだった」と気づいてコメントしたくなる内容にする""",
]



# ---------- ユーティリティ ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("threads-poster")


def load_env() -> None:
    """同じディレクトリの .env を読み込む（あれば）。"""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def html_to_plain_text(html: str) -> str:
    """記事HTMLから本文テキストを抽出。改行・空白を整える。"""
    soup = BeautifulSoup(html, "html.parser")
    # 不要要素除去
    for tag in soup(["script", "style", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    # 連続する空白を圧縮
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def first_meaningful_paragraph(text: str, min_len: int = 30) -> str:
    """本文から最初の意味のある段落を返す（短すぎる行はスキップ）。"""
    for para in text.split("\n\n"):
        para = para.strip()
        if len(para) >= min_len:
            return para
    # それでも見つからなければ全体の頭から
    return text[:200].strip()


# ---------- note RSS ----------

def fetch_note_articles(username: str) -> list[dict]:
    """note の RSS フィードから記事一覧を取得。"""

    rss_url = f"https://note.com/{username}/rss"
    log.info("noteのRSSを取得: %s", rss_url)
    parsed = feedparser.parse(rss_url)
    
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"RSS取得に失敗: {parsed.bozo_exception}")

    articles = []
    for entry in parsed.entries:
        title = entry.title.strip()

        # --- 追加: 除外チェック ---
        if any(kw in title for kw in EXCLUDE_KEYWORDS):
            log.info("除外対象の記事をスキップしました: %s", title)
            continue
        # ------------------------

        # content:encoded があれば優先、無ければ summary を使う
        content_html = ""
        if hasattr(entry, "content") and entry.content:
            content_html = entry.content[0].value
        elif hasattr(entry, "summary"):
            content_html = entry.summary

        articles.append({
            "id": getattr(entry, "id", None) or entry.link,
            "title": title,
            "url": entry.link,
            "published": getattr(entry, "published", ""),
            "summary": getattr(entry, "summary", ""),
            "content_html": content_html,
        })
    log.info("取得記事数: %d", len(articles))
    return articles


def fetch_single_article(url: str) -> dict:
    """単一記事のページを取得（--url オプション用）。"""
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text().strip() if title_tag else url
    return {
        "id": url,
        "title": title,
        "url": url,
        "published": "",
        "summary": "",
        "content_html": resp.text,
    }


def fetch_article_body(url: str) -> str:
    """noteの記事ページから本文テキストを取得する。"""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # noteの本文は .note-common-styles__textnote-body または article タグ内
        body = (
            soup.find("div", class_=lambda c: c and "body" in c)
            or soup.find("article")
            or soup.find("main")
        )
        if body:
            text = body.get_text("\n")
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text[:3000]  # Claude APIに渡す上限
    except Exception as e:
        log.warning("記事本文の取得に失敗（RSSの内容で代替します）: %s", e)
    return ""


def fetch_note_image(url: str) -> Optional[str]:
    """note記事のメタタグからog:imageのURLを取得する。"""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            return og_image["content"]
    except Exception as e:
        log.warning("画像の取得に失敗しました: %s", e)
    return None


# ---------- 履歴管理 ----------

def load_history(path: Path) -> dict:
    if not path.exists():
        return {"posts": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"posts": []}


def save_history(path: Path, history: dict) -> None:
    path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def pick_article(articles: list[dict], history: dict) -> Optional[dict]:
    """投稿対象の記事を選ぶ。

    戦略:
      1. 直近24時間で投稿済みでない記事を優先
      2. それでも全部消化済みなら、一番投稿回数が少ない記事をローテーション
    """
    if not articles:
        return None

    posted_records = history.get("posts", [])
    now_ts = time.time()
    one_day_sec = 24 * 60 * 60

    # 記事ごとの「直近24時間以内に投稿した回数」をカウント
    recent_by_article: dict[str, int] = {}
    total_by_article: dict[str, int] = {}
    for rec in posted_records:
        aid = rec.get("article_id")
        ts = rec.get("ts", 0)
        total_by_article[aid] = total_by_article.get(aid, 0) + 1
        if now_ts - ts <= one_day_sec:
            recent_by_article[aid] = recent_by_article.get(aid, 0) + 1

    # 24時間以内に投稿していない記事を抽出
    fresh = [a for a in articles if recent_by_article.get(a["id"], 0) == 0]
    if fresh:
        # 通算投稿回数が少ない順に並べて、その中からランダム
        fresh.sort(key=lambda a: total_by_article.get(a["id"], 0))
        candidates = fresh[: max(1, len(fresh) // 2)]
        return random.choice(candidates)

    # 全記事を24時間以内に投稿済みなら、最も古い投稿のものを選ぶ
    articles_sorted = sorted(
        articles,
        key=lambda a: total_by_article.get(a["id"], 0),
    )
    return articles_sorted[0]


# ---------- 投稿文生成 ----------

def build_post_text_with_claude(article: dict, api_key: str) -> str:
    """Anthropic Claude APIを使って記事からThreads投稿文を生成する。500文字以内。"""
    title = article["title"]
    url = article["url"]

    rss_plain = html_to_plain_text(article.get("content_html") or article.get("summary", ""))
    page_body = fetch_article_body(url)
    content = page_body if page_body and len(page_body) > len(rss_plain) else rss_plain
    content = content[:4000]

    prompt = f"""あなたはこの記事の著者本人です。自分が書いたnote記事をもとに、Threadsで多くの人に読まれる投稿文を一人称（私・僕・自分）で書いてください。

記事タイトル: {title}
記事の内容:
{content}

【Threadsで閲覧数を伸ばすための要件】

■ 冒頭（1〜2行）で必ず心をつかむ
- 読者が「あ、これ自分のことだ」と感じる共感フックで始める
- または「え、そうなの？」と思わせる意外な事実・数字で始める
- 例：「息子に『パパはあっち』と言われた。」「1600枚の写真を2時間半で整理した。」
- 冒頭から説明や前置きは絶対に入れない

■ 本文はストーリー形式で
- 箇条書きは使わない。自分の体験・気持ちの流れを短い文で語る
- 具体的なエピソード・数字・セリフを入れると読まれやすい
- 「なぜそうなったか」「どう感じたか」を正直に書く

■ 末尾は問いかけまたは여韻で締める
- 読者が「コメントしたい」「保存したい」と思う終わり方にする
- 例：「あなたはどうしてる？」「同じ経験がある人に届いてほしい。」

■ その他の制約
- 著者本人の言葉で書く（「この記事では〜」「著者は〜」は絶対NG）
- 500文字以内
- URLは含めない。末尾付近に「続きはプロフ欄から」を自然に入れる
- ハッシュタグ不要
- 日本語のみ

投稿文のみを出力してください（説明・前置き不要）。"""

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    generated = message.content[0].text.strip()

    if len(generated) > THREADS_MAX_LEN:
        generated = generated[: THREADS_MAX_LEN - 1] + "…"
    return generated



def build_single_post_with_claude(article: dict, api_key: str) -> str:
    """Anthropic Claude APIを使って1投稿形式のThreads投稿文を生成する（CTA付き・500文字以内）。
    4種のスタイルからランダムに選択してローテーションする。
    """
    title = article["title"]
    url = article["url"]

    rss_plain = html_to_plain_text(article.get("content_html") or article.get("summary", ""))
    page_body = fetch_article_body(url)
    content = page_body if page_body and len(page_body) > len(rss_plain) else rss_plain
    content = content[:4000]

    style = random.choice(_REGULAR_PROMPT_STYLES)
    style_label = style.split("\n")[0].replace("■ ", "")
    log.info("[Threads] プロンプト %s を使用", style_label)

    prompt = f"""あなたはこの記事の著者本人です。note記事をもとに、Threadsに投稿する文章を一人称（私）で書いてください。

記事タイトル: {title}
記事の内容:
{content}

{style}

【共通制約】
- 著者本人の言葉で書く（「この記事では〜」「著者は〜」は絶対NG）
- 430文字以内（末尾にCTAが付くため短めに）
- URLは含めない
- ハッシュタグ不要
- 日本語のみ

投稿文のみを出力してください（説明・前置き不要）。"""

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    generated = message.content[0].text.strip()

    # CTA を付加
    full_text = f"{generated}\n\n{THREADS_PROFILE_CTA}"

    # 500文字を超えた場合は本文を切り詰めてCTAを保持
    if len(full_text) > THREADS_MAX_LEN:
        max_body = THREADS_MAX_LEN - len(THREADS_PROFILE_CTA) - 3  # \n\n + …
        generated = generated[:max_body] + "…"
        full_text = f"{generated}\n\n{THREADS_PROFILE_CTA}"

    return full_text


def build_post_text_fallback(article: dict) -> str:
    """Groq APIが使えない場合のフォールバック（従来の抜粋方式）。URLは含めない。"""
    title = article["title"]
    plain = html_to_plain_text(article.get("content_html") or article.get("summary", ""))
    excerpt = first_meaningful_paragraph(plain) if plain else ""
    hook = random.choice(HOOK_TEMPLATES).format(title=title)
    cta = "続きはプロフ欄から"
    fixed_part = f"{hook}\n\n\n{cta}"
    remaining = THREADS_MAX_LEN - len(fixed_part)
    if remaining > 40 and excerpt:
        if len(excerpt) > remaining - 1:
            excerpt = excerpt[: remaining - 2].rstrip() + "…"
        text = f"{hook}\n\n{excerpt}\n{cta}"
    else:
        text = f"{hook}\n\n{cta}"
    if len(text) > THREADS_MAX_LEN:
        text = text[: THREADS_MAX_LEN - 1] + "…"
    return text


def build_post_text(article: dict, api_key: str = "") -> str:
    """投稿文を生成する。APIキーがあればClaude Haiku、なければ従来方式。"""
    if api_key:
        try:
            return build_post_text_with_claude(article, api_key)
        except Exception as e:
            log.warning("Claude API呼び出しに失敗。フォールバックを使用: %s", e)
    return build_post_text_fallback(article)


# ---------- Blogger投稿文生成 ----------

def build_blogger_post_with_claude(article: dict, api_key: str) -> tuple[str, str]:
    """Anthropic Claude APIを使ってBlogger向けのタイトルとHTML本文を生成する。"""
    title = article["title"]
    url = article["url"]

    rss_plain = html_to_plain_text(article.get("content_html") or article.get("summary", ""))
    page_body = fetch_article_body(url)
    content = page_body if page_body and len(page_body) > len(rss_plain) else rss_plain
    content = content[:4000]

    prompt = f"""あなたはこの記事の著者本人です。自分が書いたnote記事をBloggerでも発信するため、一人称（私・僕・自分）で書き直してください。

元記事タイトル: {title}
元記事URL: {url}
元記事の内容:
{content}

【要件】
- 著者本人が自分の体験・考えを語るトーンにする（「〜と感じた」「〜してみた」「〜だと思う」など）
- 第三者が紹介するような文体（「この記事では〜」「著者は〜」）は絶対に使わない
- 文体は常体（だ・である調）で統一する。敬体（です・ます調）は使わない
- 形式: HTML（<p>、<h2>、<ul>などのタグを使用可）
- 文字量: 400〜800文字程度（本文のみ、HTMLタグ除く）
- 構成: 導入（なぜこれを書いたか）→ 本題 → 元記事への誘導
- 末尾に元記事へのリンクを <a href="{url}">noteの元記事を読む</a> の形式で含める
- 日本語のみで書く

最初の行にブログ記事のタイトルを「TITLE:」から始めて書き、
その後に本文HTMLを書いてください。

例:
TITLE: 1歳育児で本当に使ってよかったもの、全部書いた
<p>...</p>"""

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    response_text = message.content[0].text.strip()

    # タイトルと本文を分離
    lines = response_text.split("\n", 1)
    if lines[0].startswith("TITLE:"):
        blog_title = lines[0].replace("TITLE:", "").strip()
        blog_body = lines[1].strip() if len(lines) > 1 else ""
    else:
        blog_title = title  # フォールバック
        blog_body = response_text

    return blog_title, blog_body


def build_blogger_post_english_with_claude(article: dict, api_key: str) -> tuple[str, str]:
    """Claude APIを使ってBlogger向けの英語タイトルとHTML本文を生成する。"""
    title = article["title"]
    url = article["url"]

    rss_plain = html_to_plain_text(article.get("content_html") or article.get("summary", ""))
    page_body = fetch_article_body(url)
    content = page_body if page_body and len(page_body) > len(rss_plain) else rss_plain
    content = content[:4000]

    prompt = f"""You are the author of this article. Based on your Japanese article below, write an English blog post for Blogger in first person.

Original article title (Japanese): {title}
Original article URL: {url}
Original article content (Japanese):
{content}

Requirements:
- Write as the author sharing your own experience and thoughts (not a third-party introduction)
- Natural, fluent English — do NOT translate literally from Japanese
- Format: HTML using <p>, <h2>, <ul> tags
- Length: 300–500 words (body text only, excluding HTML tags)
- Structure: engaging introduction → main content → link to original article
- End with: <p>Read the original article(Japanese): <a href="{url}">Read on note</a></p>
- Do not include URLs in the title

Output format — first line must be the title, then HTML body:
TITLE: [English title here]
<p>...</p>"""

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    response_text = message.content[0].text.strip()

    lines = response_text.split("\n", 1)
    if lines[0].startswith("TITLE:"):
        blog_title = lines[0].replace("TITLE:", "").strip()
        blog_body = lines[1].strip() if len(lines) > 1 else ""
    else:
        blog_title = title
        blog_body = response_text

    return blog_title, blog_body


# ---------- バズ投稿生成 ----------

def pick_article_for_viral(articles: list[dict], history: dict) -> Optional[dict]:
    """バズ投稿用の記事選択。直近3件のバズ投稿と同じ記事を避ける。"""
    if not articles:
        return None

    viral_posts = [p for p in history.get("posts", []) if p.get("post_type") == "viral"]
    recent_viral_ids = {p["article_id"] for p in viral_posts[-3:]}

    candidates = [a for a in articles if a["id"] not in recent_viral_ids]
    if not candidates:
        candidates = articles  # 全記事使い尽くした場合は制限なし

    return random.choice(candidates)


def build_viral_post_with_claude(article: dict, api_key: str, recent_posts: list[str]) -> str:
    """バズ狙いのThreads投稿文を生成する（リンクなし・感情ベース）。"""
    title = article["title"]
    url = article["url"]

    rss_plain = html_to_plain_text(article.get("content_html") or article.get("summary", ""))
    page_body = fetch_article_body(url)
    content = page_body if page_body and len(page_body) > len(rss_plain) else rss_plain
    content = content[:4000]

    recent_posts_text = "\n".join([f"・{p[:80]}…" for p in recent_posts[-5:]]) if recent_posts else "なし"

    style = random.choice(_VIRAL_PROMPT_STYLES)
    style_label = style.split("\n")[0].replace("■ ", "")
    log.info("[VIRAL] プロンプト %s を使用", style_label)

    prompt = f"""あなたはこの記事の著者本人です。記事の内容からインスピレーションを得て、Threadsに投稿する「何気ない呟き」を書いてください。

記事タイトル: {title}
記事の内容:
{content}

【直近の投稿（この切り口・トーン・フックは避けること）】
{recent_posts_text}

{style}

【共通制約】
- 「投稿しようと思って書いた文章」ではなく、思わずスマホを開いて打ち込んだような自然な呟きのトーン
- 記事の内容から「一番心に刺さる瞬間・感情・気づき」を一つだけ抜き出して深掘りする
- 読んだ人が「わかる」「私もそう」「どういうこと？」と思わず反応したくなる内容
- noteへのリンク・誘導は絶対に含めない（「続きはnoteで」「プロフ欄」等もNG）
- ハッシュタグ不要
- 300文字以内
- 日本語のみ

投稿文のみ出力してください。"""

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    generated = message.content[0].text.strip()

    if len(generated) > 300:
        generated = generated[:299] + "…"
    return generated


# ---------- Blogger API ----------

def post_to_blogger(blog_id: str, title: str, content: str) -> dict:
    """Blogger APIでブログ記事を投稿する（OAuth2リフレッシュトークン方式）。"""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN が未設定です。"
            "get_google_token.py を実行してリフレッシュトークンを取得してください。"
        )

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/blogger"],
    )
    credentials.refresh(Request())

    service = googleapiclient.discovery.build(
        "blogger", "v3", credentials=credentials, cache_discovery=False
    )
    body = {
        "title": title,
        "content": content,
    }
    log.info("Bloggerに投稿中: %s", title)
    result = service.posts().insert(blogId=blog_id, body=body, isDraft=False).execute()
    return result


# ---------- Threads API ----------

def post_to_threads(user_id: str, access_token: str, text: str) -> dict:
    """Threads APIで投稿。2段階フロー:
       1) /me/threads で media コンテナ作成
       2) /me/threads_publish で公開
    """
    # Step 1: コンテナ作成
    create_url = f"{THREADS_API_BASE}/{user_id}/threads"
    create_params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }
    log.info("Threads media コンテナを作成中…")
    r1 = requests.post(create_url, data=create_params, timeout=30)
    r1.raise_for_status()
    creation_id = r1.json().get("id")
    if not creation_id:
        raise RuntimeError(f"コンテナ作成に失敗: {r1.text}")

    # Threads APIは公開前に少し待つ必要がある
    time.sleep(3)

    # Step 2: 公開
    publish_url = f"{THREADS_API_BASE}/{user_id}/threads_publish"
    publish_params = {
        "creation_id": creation_id,
        "access_token": access_token,
    }
    log.info("投稿を公開中…")
    r2 = requests.post(publish_url, data=publish_params, timeout=30)
    r2.raise_for_status()
    return r2.json()

def post_thread_to_threads(user_id: str, access_token: str, texts: list[str], image_url: Optional[str] = None):
    """
    スレッド形式で投稿する。
    1枚目は画像付き（あれば）、2枚目以降は1枚目へのリプライとして投稿。
    """
    parent_id = None
    
    for i, text in enumerate(texts):
        # コンテナ作成パラメータ
        params = {
            "text": text,
            "access_token": access_token,
        }
        
        # 1投目かつ画像がある場合
        if i == 0 and image_url:
            params["media_type"] = "IMAGE"
            params["image_url"] = image_url
        else:
            params["media_type"] = "TEXT"
        
        # 2投目以降はリプライ先を指定
        if parent_id:
            params["reply_to_id"] = parent_id

        # 1. コンテナ作成
        create_url = f"{THREADS_API_BASE}/{user_id}/threads"
        r1 = requests.post(create_url, data=params, timeout=30)
        r1.raise_for_status()
        creation_id = r1.json().get("id")

        time.sleep(5) # APIの安定のため少し待機

        # 2. 公開
        publish_url = f"{THREADS_API_BASE}/{user_id}/threads_publish"
        r2 = requests.post(publish_url, data={"creation_id": creation_id, "access_token": access_token}, timeout=30)
        r2.raise_for_status()
        
        # 1投目のIDを保存して、次回の reply_to_id に使う
        if i == 0:
            parent_id = r2.json().get("id")
            log.info(f"1投目完了 (ID: {parent_id})")
        else:
            log.info(f"{i+1}投目のリプライ完了")
            
        time.sleep(2) # 連続投稿のレート制限対策


# ---------- メイン ----------

def main():
    parser = argparse.ArgumentParser(description="noteの記事をThreadsに自動投稿")
    parser.add_argument("--dry-run", action="store_true", help="投稿文を生成して表示するだけ")
    parser.add_argument("--url", help="特定の記事URLを指定して投稿")
    parser.add_argument("--username", help="note のユーザー名（既定は環境変数 NOTE_USERNAME）")
    parser.add_argument("--viral", action="store_true", help="バズ狙い投稿モード（50%%確率・リンクなし）")
    args = parser.parse_args()

    load_env()

    # 共通設定の読み込み
    username = args.username or os.environ.get("NOTE_USERNAME", DEFAULT_NOTE_USERNAME)
    history_path = Path(os.environ.get("POST_HISTORY_PATH", DEFAULT_HISTORY_PATH))
    user_id = os.environ.get("THREADS_USER_ID")
    access_token = os.environ.get("THREADS_ACCESS_TOKEN")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    history = load_history(history_path)

    # ── バズ投稿モード ──
    if args.viral:
        # 50%の確率でのみ投稿（不規則感を出す）dry-run時はスキップしない
        if not args.dry_run and random.random() > 0.50:
            log.info("[VIRAL] 今回はスキップ（確率判定）")
            return 0

        # ランダム待機（最大15分）で投稿時刻をバラけさせる（dry-run時はスキップ）
        if not args.dry_run:
            sleep_sec = random.randint(0, 30)
            log.info("[VIRAL] %d秒待機中…", sleep_sec)
            time.sleep(sleep_sec)

        articles = fetch_note_articles(username)
        if not articles:
            log.error("対象記事が見つかりません")
            return 1

        article = pick_article_for_viral(articles, history)
        if not article:
            log.error("投稿対象の選定に失敗しました")
            return 1

        log.info("[VIRAL] 選択された記事: %s", article["title"])

        recent_posts = [
            p.get("threads_text", "") if isinstance(p.get("threads_text"), str) else p.get("threads_text", [""])[0]
            for p in history.get("posts", [])[-10:]
        ]

        # バズ用はリンクなしのスレッド形式で生成（関数がリストを返すように調整されている前提）
        # ※もしbuild_viral_post_with_claudeが文字列を返すなら [viral_text] に包む
        viral_text = build_viral_post_with_claude(article, anthropic_api_key, recent_posts)
        thread_texts = [viral_text] if isinstance(viral_text, str) else viral_text

        if args.dry_run:
            log.info("[VIRAL/DRY-RUN] 生成されたスレッド:")
            for i, txt in enumerate(thread_texts):
                log.info(f"Post {i+1}: {txt}")
            return 0

        if not user_id or not access_token:
            log.error("THREADS_USER_ID / THREADS_ACCESS_TOKEN が未設定です。")
            return 2

        try:
            # バズ用は基本画像なしのスレッドとして投稿
            result = post_thread_to_threads(user_id, access_token, thread_texts, image_url=None)
            log.info("[VIRAL] Threadsスレッド投稿完了")
        except requests.HTTPError as e:
            log.error("[VIRAL] Threads API エラー: %s", e.response.text if e.response else e)
            return 3

        history.setdefault("posts", []).append({
            "post_type": "viral",
            "article_id": article["id"],
            "article_title": article["title"],
            "article_url": article["url"],
            "threads_text": thread_texts,
            "ts": time.time(),
            "ts_iso": datetime.now(timezone.utc).isoformat(),
        })
        save_history(history_path, history)
        log.info("[VIRAL] 履歴を更新: %s", history_path)
        return 0

    # ── 通常投稿モード ──
    # 記事取得
    if args.url:
        articles = [fetch_single_article(args.url)]
    else:
        articles = fetch_note_articles(username)

    if not articles:
        log.error("対象記事が見つかりません")
        return 1

    # 記事選択
    article = pick_article(articles, history)
    if not article:
        log.error("投稿対象の選定に失敗しました")
        return 1

    log.info("選択された記事: %s (%s)", article["title"], article["url"])

    # 認証情報の読み込み
    blogger_blog_id = os.environ.get("BLOGGER_BLOG_ID", "")
    blogger_enabled = bool(
        anthropic_api_key and blogger_blog_id
        and os.environ.get("GOOGLE_CLIENT_ID")
        and os.environ.get("GOOGLE_CLIENT_SECRET")
        and os.environ.get("GOOGLE_REFRESH_TOKEN")
    )

    # ── Threads用画像・投稿文生成 ──
    image_url = fetch_note_image(article["url"])
    post_text = build_single_post_with_claude(article, anthropic_api_key)
    thread_texts = [post_text]

    log.info("--- [Threads] 生成された投稿文 ---\n%s\n--- (%d 文字) ---", post_text, len(post_text))

    # ── Blogger用投稿文生成（日本語・英語）──
    blogger_title, blogger_body = None, None
    blogger_title_en, blogger_body_en = None, None
    if anthropic_api_key and blogger_blog_id:
        try:
            blogger_title, blogger_body = build_blogger_post_with_claude(article, anthropic_api_key)
        except Exception as e:
            log.warning("Blogger日本語投稿文の生成に失敗: %s", e)
        try:
            blogger_title_en, blogger_body_en = build_blogger_post_english_with_claude(article, anthropic_api_key)
        except Exception as e:
            log.warning("Blogger英語投稿文の生成に失敗: %s", e)

    if args.dry_run:
        log.info("[DRY-RUN] 投稿はスキップしました")
        return 0

    if not user_id or not access_token:
        log.error("THREADS_USER_ID / THREADS_ACCESS_TOKEN が未設定です。")
        return 2

    # ── Threadsへ投稿 ──
    try:
        post_thread_to_threads(user_id, access_token, thread_texts, image_url)
        log.info("Threads投稿完了")
    except requests.HTTPError as e:
        log.error("Threads API エラー: %s", e.response.text if e.response else e)
        return 3

    # ── Bloggerへ投稿（日本語）──
    blogger_result = None
    if blogger_enabled and blogger_title and blogger_body:
        try:
            blogger_result = post_to_blogger(blogger_blog_id, blogger_title, blogger_body)
            log.info("Blogger日本語投稿完了: %s", blogger_result.get("url", ""))
        except Exception as e:
            log.error("Blogger日本語投稿エラー: %s", e)

    # ── Bloggerへ投稿（英語）──
    blogger_result_en = None
    if blogger_enabled and blogger_title_en and blogger_body_en:
        time.sleep(10)
        try:
            blogger_result_en = post_to_blogger(blogger_blog_id, blogger_title_en, blogger_body_en)
            log.info("Blogger英語投稿完了: %s", blogger_result_en.get("url", ""))
        except Exception as e:
            log.error("Blogger英語投稿エラー: %s", e)

    # ── 履歴更新 ──
    history.setdefault("posts", []).append({
        "post_type": "regular",
        "article_id": article["id"],
        "article_title": article["title"],
        "article_url": article["url"],
        "threads_text": thread_texts,
        "image_url": image_url,
        "blogger_title_ja": blogger_title,
        "blogger_url_ja": blogger_result.get("url", "") if blogger_result else "",
        "blogger_title_en": blogger_title_en,
        "blogger_url_en": blogger_result_en.get("url", "") if blogger_result_en else "",
        "ts": time.time(),
        "ts_iso": datetime.now(timezone.utc).isoformat(),
    })
    save_history(history_path, history)
    log.info("履歴を更新: %s", history_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())