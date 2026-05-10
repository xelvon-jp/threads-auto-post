# scheduled-tasks 登録テンプレート

トークン取得＆ネットワーク許可リスト設定が終わったら、以下のテキストをClaudeに貼り付けてください。
そのままscheduled-taskとして登録できます。

---

## ローカル実行版（推奨）

ローカルマシンでスクリプトが動く状態であれば、cron / タスクスケジューラで動かすのが最も安定します。
詳細は SETUP.md ステップ3 オプションB を参照。

---

## scheduled-tasks 内で動かす場合

「以下の内容で scheduled-task を作成してください」とClaudeに依頼:

```
タスクID: threads-auto-post
説明: noteの記事をベースにThreadsへ自動投稿（1日5回ペース）
cron: 0 8,11,14,17,20 * * *
プロンプト:
  あなたはThreads自動投稿エージェントです。以下を実行してください:

  1. https://note.com/xelvon/rss をfeedparserで取得
  2. ~/threads-poster/post_history.json を読み込み、24時間以内に投稿していない記事を選定
     （全部投稿済みなら最も古い投稿の記事を選ぶ）
  3. 選んだ記事から Threads 向けの500文字以内の投稿文を作成
     - 構成: タイトル系のフック + 改行 + 本文の冒頭抜粋 + 改行 + 元記事URL
     - フックは下記のテンプレートからランダムに選ぶ:
       「📝 {title}」「今日のnote記事より👇\n{title}」「ふと書いた話：{title}」「{title}」「💭 {title}」
  4. THREADS_USER_ID と THREADS_ACCESS_TOKEN を使って Threads Graph API へPOST:
     - Step1: POST https://graph.threads.net/v1.0/{user_id}/threads
       params: media_type=TEXT, text={投稿文}, access_token={token}
     - 3秒待機
     - Step2: POST https://graph.threads.net/v1.0/{user_id}/threads_publish
       params: creation_id={step1のid}, access_token={token}
  5. post_history.json に投稿記録を追加して保存
  6. 投稿が成功したら、投稿文と元記事URLを通知に含める

  失敗時はエラー内容を残し、リトライはしないでください（次回の実行で別記事をピックします）。
```

---

## トークンを scheduled-tasks に渡す方法

scheduled-tasks 内では環境変数を直接持たせられないので、以下のいずれか:

1. **ローカルファイル参照**: `~/.threads-poster/credentials.json` に置いて、タスク実行時に読み込ませる
2. **Cowork のシークレット機能**（利用可能であれば）
3. **ローカル実行に切り替え**（最も安全・シンプル）

---

## 投稿頻度別の cron 例

| 頻度 | cron式 | 投稿時刻 |
|------|--------|---------|
| 1日4回 | `0 9,13,17,21 * * *` | 9, 13, 17, 21時 |
| **1日5回（推奨）** | `0 8,11,14,17,20 * * *` | 8, 11, 14, 17, 20時 |
| 1日6回 | `0 8,11,13,15,18,21 * * *` | 8, 11, 13, 15, 18, 21時 |

---

## 50日に1回のトークン更新リマインダー

別途、以下のscheduled-taskを登録すると安心です:

```
タスクID: threads-token-refresh-reminder
cron: 0 9 */50 * *
プロンプト:
  Threadsの長期アクセストークンを更新する時期です。以下のcurlを実行して新トークンを取得し、
  .env を書き換えてください:
  curl "https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=$現在のトークン"
```
