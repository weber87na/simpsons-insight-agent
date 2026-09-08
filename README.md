# Google Maps＋PTT＋Dcard 跨平台口碑分析

Windows 本機 FastAPI 應用程式。每個任務以「商家／品牌主題」為中心，可任選 Google
Maps、PTT、Dcard 至少一個來源，先蒐集並確認資料範圍，再執行情緒、面向、跨平台差異與
證據式問答分析。

Google 評論、PTT 文章、每則推／噓／箭頭、Dcard 文章與每則留言都各算一筆，整體統計逐筆
等權。星等和星等／文字衝突只計算 Google；Dcard 互動數只作熱度，不會直接推定情緒。

## 決策升級（2026-09）

新增[服務問題與小型實驗驗證 MVP](docs/validation-mvp.md)：由既有改善計畫設計最多三項校園／企業服務實驗，檢查反證、人工確認量測規格並回填前後結果，由規則判讀達標與否。需要升級至 `0006_validation`；提供獨立合成校園示範，非高科大真實評論，亦非模型成效驗證。

已加入週／月趨勢、語意抱怨主題、診斷／方案／評估／排程協作、改善工作回填、人工專家評分、品牌比較與前後觀察。勾選「自動產出改善計畫」可讓蒐集結束後以可用資料接續分析；預設仍保留人工確認。實際營運工作由人執行。

請先備份資料庫並升級至 migration `0005_decision`。詳見 [決策升級使用與設計](docs/decision-upgrade.md)、[OpView 功能探索紀錄](docs/opview-benchmark.md)。已操作公開範例與自訂單主題搜尋；企業方案試用及真人盲評尚待執行，不能據此宣稱本系統優於其他平台。

[OpView 功能差距與補強方案](docs/opview-gap-analysis.md) 區分已有程式碼、網站實測與建議開發，安排 P0 資料一致性、P1 營運分析、P2 探索與報告交付。P0～P2 已依 M1～M5 完成：V4 快照、共用篩選、日週月分析、證據下鑽、關鍵詞、比較圖表與列印匯出。使用方式見[營運分析操作文件](docs/operational-analytics.md)。

[OpView 桌面截圖參考](docs/opview-ui-reference.md) 收錄 58 張畫面、操作條件與限制；[詳細設計規格](docs/opview-implementation-spec.md) 定義共用篩選、快照版本、API 草案、介面狀態與驗收案例。原始研究與設計保留供追溯，目前可用功能及相容性以營運分析操作文件為準。

第二輪 N1～N5 增加同報告主題卡、聯集／重疊統計、完整品牌比較、來源／頻道橫條排行與排序、共用證據抽屜、PNG／JPEG圖檔、比較CSV及列印頁。參閱[議題與品牌比較操作／API文件](docs/comparison-workspace.md)。設定沿用既有JSON容器，沒有新增資料表。

## 安裝與升級

需求：Windows、Python 3.12、[uv](https://docs.astral.sh/uv/)。首次安裝：

```powershell
uv sync --no-editable
uv run --no-sync playwright install chromium
Copy-Item .env.example .env
uv run --no-sync alembic upgrade head
```

若從既有 Google-only 版本升級，先備份資料庫，再執行 migration `0004`：

```powershell
New-Item -ItemType Directory -Force data\backups | Out-Null
Copy-Item data\reviews.db data\backups\reviews-before-0004.db
uv run --no-sync alembic upgrade head
```

遷移會保留既有商家、任務、評論、分析與報告，並回填成
`google_maps/review` 及對應的 `job_sources`。`businesses.maps_url` 會改成可空，讓 PTT-only
或 Dcard-only 主題可建立。

工作目錄含中文時，Windows Python 3.12 可能以 CP950 讀取 editable 安裝的 `.pth` 而失敗，
因此上面使用非 editable 安裝。修改 Python 程式後可執行：

```powershell
uv sync --no-editable --reinstall-package simpsons-insight-agent
```

## 啟動

```powershell
uv run --no-sync simpsons-insight-agent
```

開啟 <http://127.0.0.1:8000>。建立任務分三步：

1. 輸入商家／品牌名稱、地址與別名；可選填共用搜尋關鍵字，留空時自動使用名稱與別名。
2. 勾選來源並補上平台必要設定：Google Maps 選定分店、PTT 指定看板；Dcard 可設定公開頁關鍵字搜尋、提供文章 URL 或加入匯入批次。
3. 預覽後建立任務；資料蒐集依 Google → PTT → Dcard 執行。

蒐集與分析刻意分開。只要至少一個來源有資料即可分析；部分來源失敗時，其他來源仍會繼續。
若畫面顯示「目前蒐集 314 則，因 `no_growth` 停止」，代表連續捲動後沒有再出現新內容，並非
314 是固定上限。可按「續抓」從 checkpoint 去重重試，或確認以目前資料分析。

如需 OpenAI 面向分析、摘要與問答，在 `.env` 設定 `OPENAI_API_KEY`。未設定時，本地情緒與
確定性報告仍可執行，任務會標記為 `PARTIAL`。

## 來源規則

- Google Maps：沿用最新／最相關、可見／無頭瀏覽器和最多 500 則評論。遇到 CAPTCHA 時不
  繞過；可見模式讓使用者手動完成驗證，無頭模式回報阻擋。
- PTT：只讀取公開網頁版的指定看板，以關鍵字搜尋標題並沿板內搜尋的上頁連結翻頁。
  每組看板／關鍵字預設最多 20 頁，可設 1～100 頁；預設 50 篇文章、500 則推文、每串
  最多 100 則，文章／推文硬上限為 200／2,000。
- Dcard：支援公開搜尋頁的關鍵字探索、可選看板篩選，以及指定公開文章 URL／CSV／JSON
  匯入。每組搜尋預設最多 5 頁，可設 1～20 頁；只跟隨頁面實際提供且符合原搜尋條件的
  翻頁連結。不登入、不呼叫未公開 API、不展開登入後或動態隱藏內容；公開頁內容或搜尋
  覆蓋範圍無法確認完整時記錄缺漏。尚未達到設定的文章與留言雙額度時標記 `PARTIAL`；
  `target_reached` 僅代表達到額度。

兩個論壇來源皆以單連線限速，PTT 預設請求間隔 1 秒、Dcard 預設 2 秒，遵循
`Retry-After`。跨網域、非 HTTPS 或非標準連接埠重新導向會被拒絕；不繞過 403、CAPTCHA
或 PTT 年齡確認。功能盤點、設定與停止原因見 [PTT／Dcard 蒐集操作與限制](docs/forum-collection.md)。
本次論壇補強不新增資料庫 migration；既有日期不會自動重寫，新解析資料才套用修正後的
臺灣時區與去重規則，詳見該文件的升級說明。

Dcard 使用者協議限制自動化登入／操作蒐集內容，且可能限制未經同意的商業用途。建立 Dcard
來源前必須確認提醒，並自行確保對內容具有合法使用權。另請閱讀
[PTT 網頁版](https://www.ptt.cc/bbs/index.html)、[PTT 使用者條款](https://www.ptt.cc/index.ua.html)
與 [Dcard 使用者協議](https://www.dcard.tw/terms)。

## Dcard 匯入

介面可下載 CSV 或 JSON 範本；API 也提供：

```text
GET /api/source-imports/dcard/template?format=csv
GET /api/source-imports/dcard/template?format=json
POST /api/source-imports/dcard
```

固定欄位為：

```text
item_type,source_item_id,thread_id,parent_id,title,text,published_at,forum,source_url,author,reaction_count
```

`published_at` 必須是 ISO 8601。`source_item_id` 缺少時會以 URL、討論串、日期與內容建立穩定
鍵；主文可由 URL 的文章 ID 補齊。匯入時會核對文章 ID、討論串與看板，移除完全相同的
重複列，拒絕同一 ID 內容衝突。檔案最大 10 MB；任何一列有錯都回傳 HTTP 422，不會靜默略過。

## 隱私與 OpenAI 資料邊界

PTT ID、Dcard 卡稱／校系會在解析或匯入當下，以本機 `data/author-hash.key` 做 HMAC-SHA256，
隨即丟棄原文。作者原文與雜湊都不顯示、不匯出、不傳 OpenAI。請把這把金鑰和資料庫一同
備份；遺失後，新舊作者匿名鍵將無法對應。

送往 OpenAI 的資料只包含匿名項目鍵、來源、內容類型、遮罩後標題／文字、時間與 Google
評分；不含作者雜湊或來源 URL。論壇內容一律視為不可信輸入，沿用 prompt-injection 防護。
SQLite、原文、瀏覽器 profile、模型與診斷資料都在已忽略版控的 `data/`。

## 報告與 API

新報告 payload 為 `schema_version: 3`（舊版讀取相容），提供整體及各來源統計、內容類型組成、來源蒐集狀態、
平台差異、熱門討論串和代表內容。每筆內容也會先執行本地正／中／負情感分類；只有星等沒有
文字的 Google 項目會標記為 `rating_only`。OpenAI 主要負責面向、摘要與追問。舊報告在讀取時轉接，
不會原地改寫。

主要 API：

- `POST /api/businesses/search`（相容）與 `POST /api/sources/google-maps/search`
- `POST /api/jobs`、`GET /api/jobs/{id}`、`GET /api/jobs/{id}/events`
- `POST /api/jobs/{id}/resume`、`POST /api/jobs/{id}/analyze`、`POST /api/jobs/{id}/cancel`、`DELETE /api/jobs/{id}`（刪除任務與該任務獨有的爬文資料）
- `GET /api/reports/{id}`、`GET /api/reports/{id}/items`
- `GET /api/reports/{id}/reviews`（相容別名）
- `GET /api/reports/{id}/export?format=csv|json`
- `POST /api/reports/{id}/questions`

新版建立任務範例：

```json
{
  "subject": {
    "kind": "brand",
    "name": "範例品牌",
    "aliases": ["Example"]
  },
  "sources": [
    {
      "source": "ptt",
      "boards": ["Food"],
      "keywords": ["範例品牌", "Example"],
      "date_from": "2025-08-24",
      "date_to": "2026-08-24",
      "max_posts": 50,
      "max_comments": 500,
      "max_comments_per_thread": 100,
      "max_search_pages": 20
    }
  ],
  "llm_model": "gpt-5.4-mini-2026-03-17"
}
```

舊版 Google-only `POST /api/jobs` body 仍會自動轉換並保持相容。

## 測試

```powershell
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync mypy src/simpsons_insight_agent
```

PTT 與 Dcard 解析、阻擋頁、惡意重新導向、匯入與 migration 測試預設全部使用離線 fixtures。
真實網站 smoke test 預設停用，且一次只讀一篇文章與少量留言：

```powershell
$env:RUN_LIVE_PTT_TESTS = "1"
uv run --no-sync pytest tests/test_live_forums.py -k ptt

$env:RUN_LIVE_DCARD_URL_TESTS = "1"
$env:LIVE_DCARD_URL = "https://www.dcard.tw/f/food/p/123456789"
uv run --no-sync pytest tests/test_live_forums.py -k dcard
```

本工具只綁定 `127.0.0.1`，不提供代理池、stealth、帳號輪替、反封鎖或 CAPTCHA 自動繞過。
