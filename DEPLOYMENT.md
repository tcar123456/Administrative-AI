# 部署與維運

本文適用 Python / Docker 部署。**完全放在 Cloudflare 的 Demo 請改看 [CLOUDFLARE.md](CLOUDFLARE.md)**，它不需要下列容器。

這套配置提供單一組織、單一應用程序的小型部署路徑。它沒有多副本鎖、分散式限流、MFA 或 SSO；不應直接增加 workers／app replicas。全域聊天鎖同時只接受一個 AI 任務，忙碌時回傳 409，使用者可稍後重試。

## 正式上線準備

- 準備 Linux 主機、Docker Engine／Compose、網域，DNS 指向主機，允許 80／443。Caddy 終結 HTTPS。
- 使用全新正式資料庫 volume，不把本機示範 SQLite 或測試資料直接當作公司資料。
- 建立 `company-knowledge/`，放入公司核可的 UTF-8 MD／TXT 或有文字層的 PDF。此資料夾至少有一份可索引文件；正式模式拒絕使用內建虛構政策目錄。
- 複製 `.env.production.example` 為 `.env.production`，填寫 `APP_DOMAIN` 與三個不同用途的秘密。

| 設定 | 用途與產生方式 |
|---|---|
| `POSTGRES_PASSWORD` | 資料庫密碼。使用密碼管理器產生至少 32 字元的 URL-safe 隨機值，只用英數、`_`、`-`，避免資料庫 URL 轉義問題 |
| `ENCRYPTION_KEY` | 用 `python -m app.manage new-key` 產生 Fernet 主密鑰，持久保存；用來解密資料庫中的供應商 Key |
| `SETUP_TOKEN` | 使用密碼管理器產生至少 32 字元隨機初始化代碼，供第一次建立管理員使用 |

加密主密鑰必須獨立於資料庫備份安全保管。遺失即無法解開既有供應商 Key。不要每次重啟重新產生，也不要把 `.env.production` 提交到 Git、貼到工單或傳給員工。

在主機上執行：

```sh
docker compose --env-file .env.production -f compose.production.yaml config --quiet
docker compose --env-file .env.production -f compose.production.yaml up --build -d
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 app caddy
```

使用 `config --quiet`，避免把展開後的秘密印到終端或 CI 紀錄。一般 `docker compose config` 可能包含密鑰。

開啟 `https://你的網域`，以部署者的 `SETUP_TOKEN` 建立第一位管理員。接著依 [使用者指南](USER_GUIDE.md) 設定 AI、測試工具呼叫、建立員工及正確額度。至少有兩位經授權的管理員，才能審核管理員自己的請假。

`ENVIRONMENT=production` 會檢查 HTTPS 網址、Fernet 主密鑰、關閉示範資料、PostgreSQL、Qdrant server 以及獨立知識庫目錄。Cookie 會設 Secure。API 須符合 PUBLIC_ORIGIN 及 CSRF；請使用一致的正式網域。

應用、資料庫、Qdrant 沒有對外 ports；Caddy 只公開 80／443。PostgreSQL／Qdrant 在 internal network，應用另接 edge network 以連線 AI 供應商。只有 Caddy 接觸使用者網路。Uvicorn 不信任代理提供的 Client IP，登入 IP 限流因此以代理為範圍（整體每 15 分鐘最多 20 次），另有每帳號 10 次限制；若組織人數需要放大此限制，必須先設計可信代理來源及邊界限流，不能無限制信任 X-Forwarded-For。

## 上線驗收

1. 開啟 HTTPS 正式網址，確認瀏覽器憑證正常。
2. 未登入不能取得 dashboard、文件、對話或模型列表；員工不能開啟管理 API。
3. 分別以兩個員工登入，確認個人對話、餘額、行程與請假相互隔離。
4. 每個要提供的 AI 都用真實 Key 執行「測試連線」，再以公司測試政策驗證來源與工具資料。供應商實際模型權限、區域、額度及回覆品質都要在此驗證。
5. 測試員工建立草稿 → 確認送出 → 管理員退回 → 額度返還；再測核准及不重複扣額度。
6. 測試登出、改密碼、停用帳號與伺服器重啟；Key 設定在重啟後仍須能解密。
7. 驗證備份可在隔離環境還原，完成組織要求的帳號、文件、日誌與對話保存政策。

## 備份與還原

需要一併保護：PostgreSQL（帳號、加密供應商 Key、Session、對話、請假與事件）、公司文件、ENCRYPTION_KEY 以及選用的 OpenAI embedding 設定。Qdrant 可由文件重建，重建外部 embedding 會產生 API 成本。

Linux 主機上的資料庫備份範例（先建立只允許部署者讀取的備份目錄）：

```sh
mkdir -p backups
chmod 700 backups
umask 077
docker compose --env-file .env.production -f compose.production.yaml exec -T postgres pg_dump -U daywork -d daywork -Fc > backups/daywork.dump
```

備份含敏感業務資料，需依組織規範加密保存與設定保留期限。主密鑰存於獨立秘密管理系統，不與 DB dump 放在同一個公開位置。

還原時，先在**另一個隔離的測試專案／主機**準備相同版本 app 與空的 PostgreSQL，停止 app，將 dump 以 `pg_restore -U daywork -d daywork --no-owner` 匯入空 DB，設定原本 ENCRYPTION_KEY 與原始文件，再啟動 app 重建 Qdrant。驗證登入、Key 連線及請假後才能訂定正式災難復原程序。不要未經檢查就用 `--clean` 覆蓋現有正式 DB。

`docker compose down` 保留 named volumes；`down -v` 會刪除資料，日常停止服務不要加 `-v`。

## 升級與 schema

這次改版只新增資料表，既有員工、請假、餘額與對話保留。升級前備份 DB 與主密鑰，停止舊 app，建置新版並用單一程序啟動。`Base.metadata.create_all` 只能補缺少的表；沒有自動修改舊欄位或資料的能力。未來 schema 變更要另編 migration，並在還原備份的 staging 環境驗證後部署。回退程式版本前也需確認 schema 相容。

## 密鑰輪替

供應商 Key：管理介面「更換 Key / 模型」→ 加密儲存 → 測試 → 啟用，再到供應商後台撤銷舊 Key。不要把撤銷舊 Key 和刪除本機設定混淆。

Fernet 主密鑰：本版沒有線上重加密指令，不能直接替換後沿用舊密文。需安排維護時間，備份舊密鑰及 DB，停用並移除全部 AI 設定後變更主密鑰、重啟，再由管理員重新填入各供應商 Key、測試及啟用；舊備份仍須保留其對應的舊主密鑰才能解密。

忘記登入密碼：

```sh
docker compose --env-file .env.production -f compose.production.yaml exec app python -m app.manage reset-password --username admin
```

輸入新密碼兩次，既有 Session 會撤銷。不要將密碼置於 shell 參數或工單。

## 監控、用量與資料政策

- `/api/health` 檢查 DB 與 Qdrant 索引，異常回 503；不會呼叫模型，也不證明模型可用。
- Caddy／app 有容器日誌；管理員可在介面查看最近管理事件，個人請假可看逐筆紀錄。上游模型錯誤不印完整 exception body，避免密鑰與敏感內容外洩。
- 每位使用者聊天每分鐘上限 20 次；每位管理員模型測試每分鐘上限 6 次；單次 AI 最多 6 輪、8 次工具、每次輸出最多 2048 tokens。輸入與模型推理亦可能計費，這不是金額硬上限。請在供應商專案設定預算／支出限制與通知。
- 對話每串保存最後 40 則訊息，但歷史對話筆數、請求憑證及操作事件尚無自動保留期限／刪除介面。組織若有強制到期刪除需求，應在導入前補足，不能宣稱已有資料生命週期治理。
- Session 8 小時到期；過期 Session 在登入時清理。限流保存在單一程序，重啟會清空。要更強防護需部署層監控／限流以及 SSO／MFA 整合。
- 知識庫全體登入員工可見；如需部門／文件 ACL，導入前必須擴充檢索與文件端點的一致權限檢查。
- 沒有 Google／Outlook 行事曆同步、HRIS、年度給假、假期到期、輪班或國定假日邏輯。只適用已確認固定規則及內建管理流程的情境。

## 本次驗證界線

本機功能／安全測試及三家供應商 mock HTTP 協定測試已執行，桌面／手機版 UI 已檢查。沒有使用使用者的真實 API Key，因此未宣稱已通過供應商 live 測試。正式 Compose 已做設定解析；本機 Docker daemon 未啟動，沒有跑正式容器、PostgreSQL／Qdrant server、公開 DNS 或 TLS 憑證驗收。CI 服務測試已備妥但未在本次執行，以上步驟需部署時完成。
