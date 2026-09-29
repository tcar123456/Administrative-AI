# Zeabur 部署（Demo 展示）

採用現有 Python / FastAPI 後端，在同一個 Zeabur 專案建立應用、PostgreSQL、Qdrant 三個服務。網站與 API 同網域，不需要 Workers、D1、Wrangler 或 Node 建置。

## 1. 建立資料服務

- PostgreSQL：從平台提供的 PostgreSQL 服務建立，確認已掛載持久儲存；記下使用者、密碼、資料庫、內網主機及連接埠。
- Qdrant：以自訂 Docker image `qdrant/qdrant:v1.17.0` 建立服務，設定 HTTP 6333 port，持久儲存掛載 `/qdrant/storage`。應用使用 REST，不需公開 6334。
- 兩者只用專案內網連線，無需綁定公開網域或公開 TCP 連接埠。內網主機請複製各服務「Networking → Private」的實際值，不能用 localhost，也不能只依顯示名稱猜測。

## 2. 匯入 GitHub 應用

選擇 `tcar123456/Administrative-AI` 的 `main` 分支，專案根目錄保持儲存庫根目錄。Zeabur 會偵測根目錄 `Dockerfile`；不要設定 `ZBPACK_IGNORE_DOCKERFILE=true`，也不需要覆寫成 npm start。

Docker 預設公開 8000，啟動時優先採用平台注入的 `PORT`，綁定 `0.0.0.0`。維持 **1 個 replica、1 個 worker**；目前聊天鎖與限流在單一程序內。

在應用服務綁定 Zeabur HTTPS 網域或自訂網域，然後填入下列環境變數。首次部署可能因設定未齊而停止，完成設定後重新部署即可。

| 變數 | Demo 設定 |
| --- | --- |
| `ENVIRONMENT` | `production`，保留 Secure Cookie 與 HTTPS 檢查 |
| `PUBLIC_ORIGIN` | 實際網站網址，例如 `https://your-daywork.zeabur.app`，不要加路徑 |
| `ALLOW_DEMO` | `true`，明確允許虛構政策、示範資料與無模型時的離線規則 |
| `SEED_DEMO` | `true`，初次建立示範員工、額度及行程 |
| `AGENT_MODE` | `demo`；真實 AI 由管理介面設定 |
| `DATABASE_URL` | `postgresql+psycopg://USER:PASSWORD@PRIVATE_HOST:5432/DATABASE`，全部換成實際值 |
| `QDRANT_URL` | `http://實際Qdrant內網主機:6333` |
| `KNOWLEDGE_DIR` | `/app/knowledge` |
| `EMBEDDING_MODE` | `local`，展示不需要 Embedding API Key |
| `ENCRYPTION_KEY` | 固定的 Fernet 主密鑰，依下方指令產生 |
| `SETUP_TOKEN` | 至少 32 字元隨機初始化代碼，依下方指令產生 |

資料庫 URL 必須使用本專案已安裝的 `postgresql+psycopg` driver；平台若提供 `postgres://` 或 `postgresql://`，需替換前綴。帳密含 `@`、`/`、`:` 等特殊字元時，先將帳號、密碼各自做 URL percent-encoding。

在已安裝相依套件的本機終端各執行一次，將結果分別存入平台變數與密碼管理器（Windows 可將 `python` 換成 `.\.venv\Scripts\python.exe`）：

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

主密鑰不可每次重部署重產，否則既有 AI Key 無法解密。它使用 Fernet 格式，不是先前 Workers 版的十六進位密鑰。請勿將實際密鑰或資料庫 URL 提交 Git。

## 3. 初始化與展示驗收

1. 等 PostgreSQL、Qdrant 就緒，再重新部署應用；啟動時會建立資料表與索引文件。
2. 開啟 `PUBLIC_ORIGIN` 的 HTTPS 網址，填入 `SETUP_TOKEN`，建立第一位管理員及至少 12 字元密碼。沒有預設帳密。
3. 管理員會綁定 E001，取得示範額度及行程。其餘示範員工沒有登入帳號；在管理設定新增另一個員工帳號及額度，用於送假、切換管理員審核。
4. 未設定 AI 時，畫面應顯示「離線規則模式」。依 [DEMO.md](DEMO.md) 驗證政策引用、查假、草稿送出、審核及行程 CRUD。
5. 要展示真實模型，在管理設定輸入供應商、模型 ID 與 Key，依序儲存、測試、啟用。模型呼叫失敗會回報錯誤，不會偷偷回退規則模式。
6. 重新啟動應用，確認帳號、對話、請假及新增行程仍在；設定過的模型仍能使用。資料保存在 PostgreSQL，索引在 Qdrant volume，應用容器不需保存本機 SQLite。

初始化完成後可清空 `SETUP_TOKEN`；既有帳號仍能登入。HTTPS 網址的 `/api/health` 應回應 `status: ok`。應用檢查 Host：平台探針若不能設定正式網域 Host，請使用 TCP 探針；另以正式網址監測 `/api/health`，不要放寬 Host 驗證。容器內可用以下命令檢查完整依賴（不含秘密）：

```sh
python -c "import os, urllib.request; from urllib.parse import urlsplit; req=urllib.request.Request('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health', headers={'Host':urlsplit(os.environ['PUBLIC_ORIGIN']).netloc}); print(urllib.request.urlopen(req).read().decode())"
```

## 常見問題與正式使用

- 「網站來源不正確」或登入 403：確認 `PUBLIC_ORIGIN` 與瀏覽器網址完全相同，更新變數後重新部署；全程使用 HTTPS。
- 資料服務連不上：確認三個服務在同一專案，使用 Private hostname、正確 port，資料服務已完成啟動。
- 登入 429：應用不信任任意轉送 IP，代理後的 IP 限流可能由全站共用；展示時避免連續大量登入失敗。不要用信任所有代理來繞過限制。
- Demo 與正式資料應使用獨立專案、資料庫、Qdrant 儲存及密鑰。`SEED_DEMO=false` 不會刪除既有示範資料。
- 正式使用改為 `ALLOW_DEMO=false`、`SEED_DEMO=false`，將真實文件透過受控 volume 或平台檔案管理放到獨立 `/app/company-knowledge` 並設定 `KNOWLEDGE_DIR`；確認容器的 appuser 有讀取權限。設定並啟用 AI 後才提供聊天。更多備份與維運限制見 [DEPLOYMENT.md](DEPLOYMENT.md)。

官方參考：[Dockerfile 部署](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)、[環境變數](https://zeabur.com/docs/en-US/deploy/config/environment-variables)、[專案內網](https://zeabur.com/docs/en-US/deploy/networking/private-networking)。
