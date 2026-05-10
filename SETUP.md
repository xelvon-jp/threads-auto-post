# note → Threads 自動投稿セットアップ手順

このドキュメントは `post_to_threads.py` を動かすための初期セットアップ手順です。
所要時間: 30〜45分（Meta Developers のアプリ申請待ち時間を除く）

---

## 全体像

```
note RSS  ──→  post_to_threads.py  ──→  Threads Graph API  ──→  あなたのThreads
                       ↑
                  scheduled-tasks
                  （2〜3時間ごとに自動実行）
```

---

## ステップ1: Threads アクセストークンの取得

Threads公式APIを使うには Meta for Developers でアプリ登録 → トークン取得が必要です。

### 1-1. Meta for Developers にログイン

1. https://developers.facebook.com/ にアクセスし、自分のFacebookアカウントでログイン
2. （初回のみ）開発者アカウントを有効化（電話番号認証あり）

### 1-2. アプリを作成

1. 右上「マイアプリ」→「アプリを作成」
2. ユースケースで「**その他**」を選択 → 次へ
3. アプリタイプは「**ビジネス**」を選択 → 次へ
4. アプリ名: 任意（例: `xelvon-threads-poster`）
5. 連絡先メール: あなたのメールアドレス
6. 「アプリを作成」をクリック

### 1-3. Threads製品を追加

1. 作成したアプリのダッシュボードで「**製品を追加**」
2. 「**Threads API**」を探して「設定」をクリック
3. リダイレクトURIを設定（ローカル運用なら `https://localhost/` で可）

### 1-4. アクセストークン取得

1. 左メニュー「Threads API」→「Use cases」→ 「Access the Threads API」
2. 「**Generate Access Token**」をクリック
3. 自分のThreadsアカウントで認証 → 権限を付与
4. 表示された **短期アクセストークン**（1時間有効）をコピー

### 1-5. 長期トークンに変換（重要）

短期トークンを長期（60日）に変換します。ターミナルで以下を実行：

```bash
APP_ID="あなたのアプリID"
APP_SECRET="あなたのアプリシークレット"
SHORT_TOKEN="短期アクセストークン"

curl -G "https://graph.threads.net/access_token" \
  -d "grant_type=th_exchange_token" \
  -d "client_secret=$APP_SECRET" \
  -d "access_token=$SHORT_TOKEN"
```

返ってくるJSONの `access_token` が長期トークンです。これを保存。

### 1-6. ユーザーIDの取得

```bash
LONG_TOKEN="長期トークン"
curl "https://graph.threads.net/v1.0/me?fields=id,username&access_token=$LONG_TOKEN"
```

返ってきた `id`（数値）が `THREADS_USER_ID` になります。

---

## ステップ2: スクリプトの設置

### 2-1. ファイルを任意のフォルダにコピー

`outputs/` の以下のファイルをまとめて、自動投稿用フォルダ（例: `~/threads-poster/`）に置きます。

- `post_to_threads.py`
- `requirements.txt`
- `.env.example`

### 2-2. Python依存パッケージのインストール

```bash
cd ~/threads-poster
python -m venv venv
source venv/bin/activate     # Windowsなら: venv\Scripts\activate
pip install -r requirements.txt
```

### 2-3. .env ファイル作成

```bash
cp .env.example .env
```

`.env` を編集して、ステップ1-5/1-6で取得した値を埋める：

```
THREADS_USER_ID=1234567890123456
THREADS_ACCESS_TOKEN=THAA...（長期トークン）
NOTE_USERNAME=xelvon
```

### 2-4. 動作確認（ドライラン）

```bash
python post_to_threads.py --dry-run
```

→ 投稿文が画面に表示されればOK。Threadsへは送信されません。

### 2-5. 本番投稿テスト

```bash
python post_to_threads.py
```

→ Threadsアプリで実際に投稿されているか確認。

---

## ステップ3: 自動実行のセットアップ

### オプションA: scheduled-tasks（Cowork内で完結）

**前提**: ネットワーク許可リストに以下を追加する必要があります（Settings → Capabilities）:
- `note.com`
- `graph.threads.net`

追加後、Claudeに「Threads自動投稿のscheduled-taskを登録して」と依頼してください。
2〜3時間ごとのcron式で登録します（例: 毎日 8/11/14/17/20時に実行 → `0 8,11,14,17,20 * * *`）。

### オプションB: ローカルマシンの cron / タスクスケジューラ

#### macOS / Linux (cron)

```bash
crontab -e
```

以下を追加（毎日 8/11/14/17/20 時、5本/日のペース）:

```
0 8,11,14,17,20 * * * cd ~/threads-poster && ./venv/bin/python post_to_threads.py >> threads.log 2>&1
```

#### Windows（タスクスケジューラ）

1. タスクスケジューラを起動 → 「タスクの作成」
2. トリガー: 「毎日」、繰り返し間隔「3時間」、継続時間「1日間」
3. 操作: プログラムの開始
   - プログラム: `C:\Users\yotan\threads-poster\venv\Scripts\python.exe`
   - 引数: `post_to_threads.py`
   - 開始: `C:\Users\yotan\threads-poster`

---

## ステップ4: 運用Tips

### トークンの更新

長期トークンは **60日** で失効します。失効前に以下で更新：

```bash
curl "https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=$LONG_TOKEN"
```

リマインダーとして scheduled-tasks に「50日に1回トークン更新を確認」を入れておくと安心です。

### 投稿履歴

`post_history.json` に投稿済み記事と投稿時刻が記録されます。
同じ記事は24時間以内に再投稿されない仕組みです。

### 投稿頻度の調整

- 1日4回 → cron: `0 9,13,17,21 * * *`
- 1日5回 → cron: `0 8,11,14,17,20 * * *`（推奨）
- 1日6回 → cron: `0 8,11,13,15,18,21 * * *`

### スパム判定を避けるコツ

- 同じ記事の連投は避ける（スクリプトが自動で防止）
- ハッシュタグの乱用は控える
- 文面のバリエーションを増やす（HOOK_TEMPLATES を編集）
- たまに手動投稿も混ぜる（完全自動化アカウントは不自然に見える）

---

## トラブルシューティング

| 症状 | 確認ポイント |
|------|---|
| `THREADS_USER_ID が未設定` | `.env` の値が空、または環境変数が読み込まれていない |
| `401 Unauthorized` | トークン失効。リフレッシュするか再発行 |
| `403 Forbidden` | アプリのレビュー未通過。テストユーザーでのみ投稿可能なケースあり |
| `RSS取得に失敗` | noteのURL/ユーザー名を確認、ネットワーク許可リストを確認 |
| 投稿が反映されない | コンテナ作成→公開の間に3秒待機しているが、APIの混雑時は5〜10秒に伸ばす |

---

## サポート

困ったら、エラーメッセージ全文と `python post_to_threads.py --dry-run` の出力をClaudeに貼り付けてください。
