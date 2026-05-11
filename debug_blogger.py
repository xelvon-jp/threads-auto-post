"""
Blogger API デバッグスクリプト
================================
現在のOAuth2認証情報で「どのアカウントが認証されているか」と
「アクセスできるブログ一覧」を確認します。

実行方法:
  python debug_blogger.py
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import googleapiclient.discovery

load_dotenv(Path(__file__).parent / ".env")

client_id = os.environ.get("GOOGLE_CLIENT_ID")
client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
blogger_blog_id = os.environ.get("BLOGGER_BLOG_ID")

print("=== 認証情報の確認 ===")
print(f"GOOGLE_CLIENT_ID: {client_id[:20]}..." if client_id else "未設定")
print(f"GOOGLE_CLIENT_SECRET: {client_secret[:10]}..." if client_secret else "未設定")
print(f"GOOGLE_REFRESH_TOKEN: {refresh_token[:20]}..." if refresh_token else "未設定")
print(f"BLOGGER_BLOG_ID: {blogger_blog_id}")
print()

credentials = Credentials(
    token=None,
    refresh_token=refresh_token,
    token_uri="https://oauth2.googleapis.com/token",
    client_id=client_id,
    client_secret=client_secret,
    scopes=["https://www.googleapis.com/auth/blogger"],
)

print("=== トークンを更新中... ===")
credentials.refresh(Request())
print(f"アクセストークン取得成功: {credentials.token[:30]}...")
print()

# トークンのスコープ確認 + 認証アカウントのメール取得
import requests as req_lib
print("=== トークンの実際のスコープ確認 ===")
try:
    resp = req_lib.get(
        "https://www.googleapis.com/oauth2/v3/tokeninfo",
        params={"access_token": credentials.token},
        timeout=10,
    )
    info = resp.json()
    print(f"  付与スコープ: {info.get('scope', '不明')}")
    if "blogger" in info.get("scope", "") and "readonly" not in info.get("scope", ""):
        print("  ✅ 書き込みスコープあり")
    else:
        print("  ❌ 書き込みスコープなし（読み取り専用）← これが原因！")
except Exception as e:
    print(f"  スコープ確認エラー: {e}")

# userinfo エンドポイントで認証アカウントのメールを取得
print()
print("=== 認証アカウントの確認 ===")
try:
    resp2 = req_lib.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=10,
    )
    if resp2.status_code == 200:
        userinfo = resp2.json()
        email = userinfo.get("email", "不明")
        print(f"  認証アカウント: {email}")
        if email == "xelvon.jp@gmail.com":
            print("  ✅ 正しいアカウントです")
        else:
            print(f"  ⚠️  想定とは異なるアカウントです！（期待: xelvon.jp@gmail.com）")
            print(f"     → get_google_token.py を再実行して xelvon.jp@gmail.com でログインしてください")
    else:
        print(f"  userinfo取得失敗（emailスコープが必要）: status={resp2.status_code}")
        print(f"  → get_google_token.py を実行して emailスコープを追加します")
except Exception as e:
    print(f"  エラー: {e}")
print()

service = googleapiclient.discovery.build(
    "blogger", "v3", credentials=credentials, cache_discovery=False
)

# 自分のブログ一覧を取得
print("=== このアカウントがアクセスできるブログ一覧 ===")
try:
    blogs = service.blogs().listByUser(userId="self").execute()
    items = blogs.get("items", [])
    if not items:
        print("⚠️  ブログが見つかりません。このアカウントにはBloggerブログがないようです。")
    for blog in items:
        print(f"  - ブログ名: {blog['name']}")
        print(f"    ID: {blog['id']}")
        print(f"    URL: {blog.get('url', '不明')}")
        if blog['id'] == blogger_blog_id:
            print(f"    ✅ これが設定中のBLOGGER_BLOG_IDと一致します！")
        print()
except Exception as e:
    print(f"エラー: {e}")

# 対象ブログへのアクセス確認
print(f"=== 対象ブログ（ID: {blogger_blog_id}）へのアクセス確認 ===")
try:
    blog = service.blogs().get(blogId=blogger_blog_id).execute()
    print(f"✅ ブログ取得成功: {blog['name']} ({blog.get('url', '')})")
except Exception as e:
    print(f"❌ エラー: {e}")

# ユーザーのブログに対するロール確認（生レスポンス表示）
print()
print("=== ユーザーのブログに対するロール確認（生レスポンス）===")
try:
    user_info = service.blogUserInfos().get(userId="self", blogId=blogger_blog_id).execute()
    print(f"  生レスポンス: {user_info}")
    blog_user = user_info.get("blogUserInfo", {})
    role = blog_user.get("role", "不明")
    has_admin = blog_user.get("hasAdminAccess", "不明")
    print(f"  ロール: {role}")
    print(f"  hasAdminAccess: {has_admin}")
except Exception as e:
    print(f"  エラー: {e}")

# 直接HTTPリクエストで書き込みテスト（より詳細なエラー情報）
print()
print("=== 直接HTTPリクエストによる書き込みテスト ===")
try:
    import json as json_lib
    post_url = f"https://blogger.googleapis.com/v3/blogs/{blogger_blog_id}/posts?isDraft=true"
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }
    body = json_lib.dumps({"title": "[テスト削除OK]", "content": "<p>test</p>"})
    resp_post = req_lib.post(post_url, headers=headers, data=body, timeout=15)
    print(f"  HTTPステータス: {resp_post.status_code}")
    print(f"  レスポンス（全文）:\n{resp_post.text}")
except Exception as e:
    print(f"  エラー: {e}")

# 投稿一覧の取得テスト（読み取り）
print()
print("=== 投稿一覧の読み取りテスト ===")
try:
    posts = service.posts().list(blogId=blogger_blog_id, maxResults=1).execute()
    print(f"  ✅ 投稿一覧取得成功（{len(posts.get('items', []))}件）")
except Exception as e:
    print(f"  ❌ エラー: {e}")

# 実際に下書き投稿を試す（書き込み権限の確認）
print()
print("=== 書き込みテスト（下書き投稿）===")
try:
    test_post = service.posts().insert(
        blogId=blogger_blog_id,
        body={"title": "[テスト] 自動投稿テスト（削除してください）", "content": "<p>テスト投稿です。削除してください。</p>"},
        isDraft=True,
    ).execute()
    print(f"✅ 下書き投稿成功！ → 後で手動削除してください")
    print(f"   投稿URL: {test_post.get('url', '不明')}")
    print(f"   投稿ID: {test_post.get('id', '不明')}")
except Exception as e:
    print(f"❌ 書き込みエラー: {e}")
