# 完全部署到 Cloudflare：Workers + D1

此版本讓網站、API 與持久資料全部放在 Cloudflare，不需要自己的主機、Tunnel、Docker、PostgreSQL 或 Qdrant。與 Python 版本共用 static/ 介面，Cloudflare 後端在 cloudflare/。外部聊天模型仍使用供應商 API Key，沒有 Key 時可使用明確標示的離線規則展示。

## 架構與範圍

| 功能 | Cloudflare Demo |
|---|---|
| 前端與 API | Workers Static Assets + JavaScript Worker |
| 帳號、對話、請假、行程、模型設定 | D1；版本化 SQL migration |
| 額度一致性 | 請假狀態、扣還額度、事件在同一 SQL trigger 交易內完成 |
| 登入 | D1 Session、HttpOnly / Secure / SameSite Cookie、Origin + CSRF |
| 密碼與 Key | PBKDF2-SHA256（100,000 次、隨機 salt）；AES-256-GCM 加密供應商 Key |
| 限流與聊天鎖 | D1 保存；每帳號聊天鎖；失效 Session／限流／鎖每日清理 |
| 文件 | 建置時打包 MD/TXT，以中文 bigram 詞彙比對；文件 API 需登入 |
| AI | OpenAI Responses、Claude Messages、Gemini 相容介面；四個白名單工具 |

Cloudflare 與 Python 使用不同資料庫 schema、密碼格式和加密格式，**不會自動搬移本機資料或密鑰**。部署時建立全新 D1。Cloudflare 不使用 Qdrant 或外部語意 embedding；PDF 請先轉成 UTF-8 MD/TXT。

此版本以單一組織、少量展示資料為範圍。所有登入者可閱讀展示文件；未實作文件分級、SSO/MFA、年度給假、外部行事曆同步、D1 密碼復原 CLI。對話每串保留最後 40 則，但歷史筆數與重試憑證無自動刪除，需另定保存政策。Python 的 app.manage 指令不適用 D1。

## 本機試跑

使用 Node.js 24 LTS，於專案根目錄執行 `npm ci`，建立不會提交 Git 的 `.dev.vars`：

```dotenv
ENVIRONMENT=local
PUBLIC_ORIGIN=http://localhost:8787
ALLOW_DEMO=true
SETUP_TOKEN=至少32字元的隨機初始化代碼
ENCRYPTION_KEY=64位十六進位隨機字串
```

下面指令每次會產生不同的 64 位字串。執行兩次，分別用於上述兩個秘密，保存在密碼管理器：

```sh
node -e "console.log(require('node:crypto').randomBytes(32).toString('hex'))"
npm run db:local
npm run dev
```

開啟 **http://localhost:8787**，必須與 PUBLIC_ORIGIN 完全相同。使用初始化代碼建立管理員，沒有預設帳密。本機 D1 位於 `.wrangler/state/`，重啟仍保留；它與遠端資料分開。

## 雲端部署

1. 登入並建立資料庫：

```sh
npx wrangler login
npx wrangler d1 create daywork-demo
```

2. 將回傳的 database_id 填入 `wrangler.jsonc`，替換全零佔位值；該 ID 不是密碼。確認帳號的 Workers 子網域，將 PUBLIC_ORIGIN 改為完整 HTTPS 網址，例如 `https://daywork-demo.your-subdomain.workers.dev`。若使用自訂網域，也要同步修改。雲端保持 ENVIRONMENT=production，不上傳 `.dev.vars`。

3. 套用雲端 schema，再設定兩個不同的隨機秘密：

```sh
npm run db:remote
npx wrangler secret put SETUP_TOKEN
npx wrangler secret put ENCRYPTION_KEY
```

依提示輸入秘密。主密鑰須為 **64 位十六進位**，不是 Python 的 Fernet key。後續部署必須沿用原本密鑰，否則不能解密已保存的供應商 Key。

4. 測試並部署：

```sh
npm test
npm run check
npm run deploy
```

Wrangler 先執行 build，再上傳 Worker 與靜態資源。開啟 HTTPS 網址，用雲端 SETUP_TOKEN 初始化管理員。

**請建立 Workers 專案，不要只把 static/ 上傳到 Pages。** 若之後接 GitHub Workers Builds，根目錄使用本專案根目錄、build command 用 `npm run build`、deploy command 用 `npx wrangler deploy`；D1 binding、Secrets 與首次 migration 仍要先完成。一般 push 的 GitHub Actions 只測試，不自動部署。

## 準備展示

- ALLOW_DEMO=true 時，首位管理員有 36 小時特休、12 小時補休；不預先建立其他帳號或會議。管理員新增展示員工並填額度，使用者自行建立行程。
- 員工在行事曆新增下週會議，問助理「我下週有哪些會議？」。
- 問「國外出差一天餐費可以報多少？」；點引用查看標示的原文段落。
- 員工建立請假並確認送出；管理員開啟審核工作台，展示待辦、搜尋與核准／退回紀錄。管理員不能自審。
- 真實 AI：儲存 Key → 測試連線 → 啟用。未設定 Key 時畫面明確標示離線規則，不宣稱是真實模型推理。
- 若只允許真實 AI，把 ALLOW_DEMO 設為 false 後重新部署；無可用模型時聊天拒絕服務。這不會清除既有展示額度與資料。

## 文件更新與維護

預設打包 knowledge/ 的六份虛構政策。修改 MD/TXT 後重新部署即更新引用版本；也可在 build 環境設定 KNOWLEDGE_DIR 指向獨立目錄。文件在 Worker bundle 中，不放入公開 static assets。部署前確認內容可供所有展示帳號閱讀。

程式升級用 `npm run deploy`；有新 migration 才先跑 `npm run db:remote`，不需要刪掉 D1。資料與主密鑰需分別備份，並測試還原。不要把本機驗證帳密用於公開展示。

## 驗證界線

- `npm test` 使用 Node SQLite 執行同一 schema／trigger，驗證權限、CSRF、交易、隔離、重試、加密與三家 AI mock 協定。
- `npm run db:local` + `npm run dev` 在 Wrangler/workerd 的本機 D1 執行；瀏覽器驗證展示流程。
- `npx wrangler deploy --dry-run --outdir .generated/worker` 驗證打包，不發布。
- `/api/health` 檢查 D1 與文件，不呼叫模型。
- AI 每次最多 6 輪、8 個工具、每輪輸出最多 2048 tokens、90 秒處理期限；這不是金額硬上限，應在供應商設定預算。
- 尚未在使用者 Cloudflare 帳戶公開部署，也未以真實 API Key 連線；本機驗證不等同雲端驗收。部署時仍須驗證 HTTPS、重新部署後資料保存、兩個帳號隔離與方案用量。

官方參考：[Static Assets](https://developers.cloudflare.com/workers/static-assets/binding/)、[D1 batch 交易](https://developers.cloudflare.com/d1/worker-api/d1-database/)、[Web Crypto](https://developers.cloudflare.com/workers/runtime-apis/web-crypto/)。
