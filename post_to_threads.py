"""
note の記事から Threads へ自動投稿するスクリプト
================================================

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
    """note の RSS フィードから記事一覧を取得。

    Returns:
        [{"id": guid, "title": ..., "url": ..., "published": ..., "summary": ..., "content_html": ...}, ...]
    """
    rss_url = f"https://note.com/{username}/rss"
    log.info("noteのRSSを取得: %s", rss_url)
    parsed = feedparser.parse(rss_url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"RSS取得に失敗: {parsed.bozo_exception}")

    articles = []
    for entry in parsed.entries:
        # content:encoded があれば優先、無ければ summary を使う
        content_html = ""
        if hasattr(entry, "content") and entry.content:
            content_html = entry.content[0].value
        elif hasattr(entry, "summary"):
            content_html = entry.summary

        articles.append({
            "id": getattr(entry, "id", None) or entry.link,
            "title": entry.title.strip(),
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

    prompt = f"""あなたはこの記事の著者本人です。自分が書いたnote記事をThreadsで紹介する投稿文を、一人称（私・僕・自分）で書いてください。

記事タイトル: {title}
記事の内容:
{content}

【要件】
- 著者本人が自分の言葉で発信するトーンにする（「〜を書きました」「〜について考えた」など）
- 第三者が紹介するような文体（「この記事では〜」「著者は〜」）は絶対に使わない
- 500文字以内
- 記事の核心や自分なりの気づきを一言で伝え、読者が「続きを読みたい」と思わせる
- URLは絶対に含めない。代わりに末尾に「続きはプロフ欄のnoteから」という一文を入れる
- ハッシュタグは不要
- 日本語のみで書く

投稿文のみを出力してください（説明や前置きは不要）。"""

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


def build_post_text_fallback(article: dict) -> str:
    """Groq APIが使えない場合のフォールバック（従来の抜粋方式）。URLは含めない。"""
    title = article["title"]
    plain = html_to_plain_text(article.get("content_html") or article.get("summary", ""))
    excerpt = first_meaningful_paragraph(plain) if plain else ""
    hook = random.choice(HOOK_TEMPLATES).format(title=title)
    cta = "続きはプロフ欄のnoteから"
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
- 形式: HTML（<p>、<h2>、<ul>などのタグを使用可）
- 文字量: 400〜800文字程度（本文のみ、HTMLタグ除く）
- 構成: 導入（なぜこれを書いたか）→ 本題 → noteの元記事への誘導
- 末尾に元記事へのリンクを <a href="{url}">元記事を読む</a> の形式で含める
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

    prompt = f"""You are the author of this article. Based on your Japanese note article below, write an English blog post for Blogger in first person.

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
- End with: <p>Read the original article (Japanese): <a href="{url}">here</a></p>
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


# ---------- メイン ----------

def main():
    parser = argparse.ArgumentParser(description="noteの記事をThreadsに自動投稿")
    parser.add_argument("--dry-run", action="store_true", help="投稿文を生成して表示するだけ")
    parser.add_argument("--url", help="特定の記事URLを指定して投稿")
    parser.add_argument("--username", help="note のユーザー名（既定は環境変数 NOTE_USERNAME）")
    args = parser.parse_args()

    load_env()

    username = args.username or os.environ.get("NOTE_USERNAME", DEFAULT_NOTE_USERNAME)
    history_path = Path(os.environ.get("POST_HISTORY_PATH", DEFAULT_HISTORY_PATH))

    # 記事取得
    if args.url:
        articles = [fetch_single_article(args.url)]
    else:
        articles = fetch_note_articles(username)

    if not articles:
        log.error("対象記事が見つかりません")
        return 1

    # 記事選択
    history = load_history(history_path)
    article = pick_article(articles, history)
    if not article:
        log.error("投稿対象の選定に失敗しました")
        return 1

    log.info("選択された記事: %s (%s)", article["title"], article["url"])

    # 認証情報の読み込み
    user_id = os.environ.get("THREADS_USER_ID")
    access_token = os.environ.get("THREADS_ACCESS_TOKEN")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    blogger_blog_id = os.environ.get("BLOGGER_BLOG_ID", "")
    blogger_enabled = bool(
        anthropic_api_key and blogger_blog_id
        and os.environ.get("GOOGLE_CLIENT_ID")
        and os.environ.get("GOOGLE_CLIENT_SECRET")
        and os.environ.get("GOOGLE_REFRESH_TOKEN")
    )

    # ── Threads用投稿文生成 ──
    post_text = build_post_text(article, api_key=anthropic_api_key)
    log.info("--- [Threads] 生成された投稿文 ---\n%s\n--- (%d 文字) ---", post_text, len(post_text))

    # ── Blogger用投稿文生成（日本語・英語）──
    blogger_title, blogger_body = None, None
    blogger_title_en, blogger_body_en = None, None
    if anthropic_api_key and blogger_blog_id:
        try:
            blogger_title, blogger_body = build_blogger_post_with_claude(article, anthropic_api_key)
            log.info("--- [Blogger JA] タイトル ---\n%s", blogger_title)
            log.info("--- [Blogger JA] 本文 ---\n%s\n---", blogger_body)
        except Exception as e:
            log.warning("Blogger日本語投稿文の生成に失敗: %s", e)
        try:
            blogger_title_en, blogger_body_en = build_blogger_post_english_with_claude(article, anthropic_api_key)
            log.info("--- [Blogger EN] Title ---\n%s", blogger_title_en)
            log.info("--- [Blogger EN] Body ---\n%s\n---", blogger_body_en)
        except Exception as e:
            log.warning("Blogger英語投稿文の生成に失敗: %s", e)

    if args.dry_run:
        log.info("[DRY-RUN] 投稿はスキップしました")
        return 0

    if not user_id or not access_token:
        log.error(
            "THREADS_USER_ID / THREADS_ACCESS_TOKEN が未設定です。"
            ".env を作成するか環境変数で設定してください。"
        )
        return 2

    # ── Threadsへ投稿 ──
    try:
        result = post_to_threads(user_id, access_token, post_text)
        log.info("Threads投稿完了: %s", result)
    except requests.HTTPError as e:
        log.error("Threads API エラー: %s / %s", e, e.response.text if e.response else "")
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
        try:
            blogger_result_en = post_to_blogger(blogger_blog_id, blogger_title_en, blogger_body_en)
            log.info("Blogger英語投稿完了: %s", blogger_result_en.get("url", ""))
        except Exception as e:
            log.error("Blogger英語投稿エラー: %s", e)

    # ── 履歴更新 ──
    history.setdefault("posts", []).append({
        "article_id": article["id"],
        "article_title": article["title"],
        "article_url": article["url"],
        "threads_text": post_text,
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
