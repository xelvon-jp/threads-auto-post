"""
Google OAuth2 リフレッシュトークン取得スクリプト
================================================
このスクリプトは一度だけ実行します。
ブラウザが開くので、Bloggerを所有しているGoogleアカウントでログインしてください。
取得したリフレッシュトークンが自動的に .env に追記されます。

実行方法:
  python get_google_token.py
"""

import os
import re
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv

SCOPES = ["https://www.googleapis.com/auth/blogger"]
ENV_PATH = Path(__file__).parent / ".env"


def main():
    load_dotenv(ENV_PATH)

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("エラー: .env に GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET を設定してください。")
        return

    # クライアント設定を辞書で渡す
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("ブラウザが開きます。Bloggerを所有しているGoogleアカウントでログインしてください。")
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    credentials = flow.run_local_server(port=0)

    refresh_token = credentials.refresh_token
    if not refresh_token:
        print("エラー: リフレッシュトークンが取得できませんでした。もう一度試してください。")
        return

    print(f"\nリフレッシュトークン取得成功！")

    # .env にリフレッシュトークンを追記 or 更新
    env_text = ENV_PATH.read_text(encoding="utf-8")

    if "GOOGLE_REFRESH_TOKEN=" in env_text:
        # 既存の行を更新
        env_text = re.sub(
            r"^GOOGLE_REFRESH_TOKEN=.*$",
            f"GOOGLE_REFRESH_TOKEN={refresh_token}",
            env_text,
            flags=re.MULTILINE,
        )
    else:
        # 末尾に追記
        env_text = env_text.rstrip("\n") + f"\nGOOGLE_REFRESH_TOKEN={refresh_token}\n"

    ENV_PATH.write_text(env_text, encoding="utf-8")
    print(".env にリフレッシュトークンを保存しました。")
    print("このスクリプトは今後実行不要です。")


if __name__ == "__main__":
    main()
