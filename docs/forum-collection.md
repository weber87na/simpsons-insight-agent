# PTT／Dcard 蒐集操作與限制

本次以現有 HTTP 蒐集器、跨來源任務、匿名化與 CSV／JSON 匯入為基礎補強。
Dcard 診斷中的 `collection_scope: public_html` 代表任務使用公開 HTML 及其中的
結構化資料，可能同時包含匯入檔；僅匯入時為 `import_only`。兩者均不代表平台全量資料。

## 現有功能盤點與補強

| 項目 | 原有功能／缺口 | 本次補強 |
| --- | --- | --- |
| PTT 文章探索 | 指定多看板、多關鍵字搜尋標題，固定最多 20 頁 | 搜尋頁數可設定；限制翻頁路徑與原搜尋條件，防止循環翻頁 |
| PTT 內容 | 主文、推／噓／箭頭、作者匿名鍵、日期、篇數與推文上限 | 修正臺灣時區與跨年推文日期；推文鍵不再依賴 IP／全串樓層，續抓維持每串上限 |
| Dcard 文章探索 | 只接受指定文章 URL 或匯入檔 | 新增公開搜尋頁關鍵字探索、看板篩選與有限翻頁，保留 URL／匯入模式 |
| Dcard 內容 | 解析頁面 JSON、主文與留言；JSON 記錄可能混入其他文章 | 僅採納目標討論串資料；區分完整正文與摘要，保守判定留言完整性 |
| Dcard 匯入 | CSV／JSON、日期驗證、匿名化、單列出錯拒絕整檔 | 核對文章／討論串／看板一致性；移除相同列並拒絕同 ID 衝突，補齊可由 URL 確認的值 |
| 網路錯誤 | 單連線、限速、一次 429 重試、阻擋頁與重新導向檢查 | 區分刪文與暫時失敗；有限重試並保留可用資料 |
| 範圍與狀態 | 有上限、去重、checkpoint 與來源狀態，但未充分區分截斷原因 | 補上搜尋／文章解析診斷，明確記錄上限、缺漏與停止原因 |
| 介面與驗證 | 來源選擇、日期、文章／留言上限、匯入範本 | 新增搜尋頁數、Dcard 關鍵字與看板欄位，API 與前端共同驗證 |

既有報告、來源分開統計、證據下鑽、作者 HMAC 匿名化及 OpenAI 隱私邊界繼續適用。
搜尋結果頁的文章摘要用來探索連結，不會直接當成完整文章加入分析。

## 介面使用

1. 填入商家／品牌名稱與共用搜尋關鍵字；共用欄位留空時使用名稱與別名。
2. PTT 指定看板，例如 `Food, Lifeismoney`，再設定日期及搜尋頁數。
3. Dcard 可直接填入專用關鍵字，或勾選「沿用上方搜尋詞」。勾選時若已有 Dcard 專用
   關鍵字，優先使用專用值。可選填 `food, mood` 等看板代稱，限制新探索的文章。
4. 也可僅貼上 Dcard 文章 URL 或加入合法取得的 CSV／JSON；不填 Dcard 關鍵字且未勾選
   沿用搜尋詞時，維持 URL／匯入模式。指定 URL 與匯入資料不受探索看板篩選影響。
5. 檢查預覽、來源條款確認及篇數／留言上限，再建立任務。蒐集後先檢查各來源停止原因，
   再決定續抓或使用現有資料分析。

## 設定範圍

| 欄位 | PTT | Dcard |
| --- | --- | --- |
| `boards`／`forums` | `boards` 必填，最多 10 個 | `forums` 可空，最多 10 個；使用看板代稱，轉為小寫 |
| `keywords` | 必填，最多 10 個 | 可空，最多 10 個；與 `urls`、`import_ids` 至少一種有值 |
| 每個關鍵字 | 1～100 字，去除頭尾空白與重複 | 同左 |
| `max_search_pages` | 預設 20；1～100 | 預設 5；1～20 |
| `max_posts` | 預設 50；1～200 | 預設 50；1～100 |
| `max_comments` | 預設 500；0～2,000 | 同左 |
| `max_comments_per_thread` | 預設 100；0～100 | 同左 |
| `date_from`／`date_to` | 預設最近一年至今天；起日不得晚於迄日 | 同左 |
| `acknowledge_terms` | 無此欄位 | 必須為 `true` |

`max_search_pages` 是每組看板／關鍵字的搜尋頁數上限，不包含文章頁。
多個看板與關鍵字會增加搜尋請求；跨搜尋結果的相同文章會去重。
文章數、留言總數與每串留言數是上限，不保證一定有足夠內容達到設定值。

可在 `.env` 調整請求逾時及暫時性失敗的重試次數：

```dotenv
SOURCE_HTTP_TIMEOUT_SECONDS=30
SOURCE_HTTP_RETRIES=2
PTT_REQUEST_INTERVAL_SECONDS=1
DCARD_REQUEST_INTERVAL_SECONDS=2
```

PTT 間隔可設 0.5～10 秒，Dcard 可設 1～20 秒。`SOURCE_HTTP_RETRIES` 範圍為 0～3，
用於連線／逾時及 HTTP 500／502／503／504。HTTP 429 另外依 `Retry-After` 最多重試
一次，要求等待超過 60 秒時停止；等待期間可取消任務。404／410 作為文章不存在處理，
不等同整個來源被封鎖。所有請求仍套用單連線限速與網址檢查。

## API 範例

以下 body 可送往 `POST /api/jobs`。這是指定範圍的示例，不表示範例品牌一定有搜尋結果：

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
      "boards": ["Food", "Lifeismoney"],
      "keywords": ["範例品牌", "Example"],
      "date_from": "2026-01-01",
      "date_to": "2026-09-07",
      "max_posts": 50,
      "max_comments": 500,
      "max_comments_per_thread": 100,
      "max_search_pages": 20
    },
    {
      "source": "dcard",
      "keywords": ["範例品牌", "Example"],
      "forums": ["food"],
      "urls": [],
      "import_ids": [],
      "date_from": "2026-01-01",
      "date_to": "2026-09-07",
      "max_posts": 50,
      "max_comments": 500,
      "max_comments_per_thread": 100,
      "max_search_pages": 5,
      "acknowledge_terms": true
    }
  ]
}
```

Dcard 的 `keywords` 不會由 API 自動補成主題名稱；API 呼叫端應明確提供。若只想處理指定
文章，省略 `keywords`／`forums` 並提供 `urls` 或有效的 `import_ids` 即可。
匯入欄位、範本下載及 10 MB 限制見 [README 的 Dcard 匯入](../README.md#dcard-匯入)。

Dcard 文章 URL 保持 `https://www.dcard.tw/f/{forum}/p/{id}` 格式。常見分享追蹤參數
（`utm_*`、`cid`、`ref`、`referrer`、`source`、`share`、`fbclid`、`gclid`）與片段錨點
會移除，以統一同一文章；未知查詢參數、使用者密碼及不允許的網域／連接埠會拒絕。
匯入中缺少的主文 ID、討論串與看板可由文章 URL 補齊；明確填入但不相符時整檔拒絕。

## 公開頁能提供的範圍

PTT 只搜尋指定看板標題，不提供全站全文搜尋。Dcard 公開搜尋探索只追蹤頁面提供的文章
連結與同一搜尋條件的有效翻頁連結；不猜測頁碼、不呼叫內部 API。看板篩選留空時可探索
跨看板搜尋結果，但不能因此聲稱取得全站所有相關文章。

Dcard 頁面可能只包含摘要、部分留言或需要動態載入的搜尋結果。沒有下一頁連結，不代表
沒有其他文章；顯示互動數也不代表所有留言已載入。這些狀況會記錄對應缺漏原因；未達
設定的文章與留言雙額度時為 `PARTIAL`，不能將摘要或缺漏當作完整觀測。

`collection_complete` 表示目前設定及可驗證蒐集範圍的完成狀態，不表示平台全量覆蓋，
也不能把文章上限當成該品牌的真實討論總量。PTT／Dcard 若文章與回應兩項額度都達到，
可標示 `target_reached`；過程中遇到的缺漏仍保存在 `partial_reasons`，不代表從未發生
缺漏。若只達到文章上限，仍缺少要求的回應則維持 `PARTIAL`。Dcard 公開端點在本次
執行中遇到阻擋或網路失敗，即使匯入後達到額度也保留該失敗狀態。
續抓會利用既有匿名項目鍵去重；搜尋排名或網站內容變動時，重跑結果可能不同。

日期篩選以臺灣時間的文章日期為準，範圍包含起日及迄日；符合條件的討論串再收集其留言。
無法確認主文日期的公開頁會略過並回報缺漏，避免把日期不明的內容混入指定期間。
Dcard 匯入若僅含留言，則依留言日期篩選。
PTT 推文只提供月日與時間，年份依主文日期推估；跨年已納入處理，但多年後同月日的留言
仍無法只靠原頁資訊保證精確年份。PTT 已完成的文章會在相同設定續抓時略過；續抓主要
用來完成中斷蒐集，需要重新觀察新推文時應建立新任務。

## 停止原因與診斷

任務頁及 `GET /api/jobs/{id}` 的來源結果提供狀態、`stop_reason` 與錯誤；
API 的 `sources[].diagnostics` 提供允許公開的計數、`partial_reasons` 與
`collection_scope`。完整 checkpoint 保存於本機資料庫供續抓使用，診斷回應不包含
內部游標、文章網址清單或討論串 ID。完整性、停止原因和缺漏診斷應一起判讀。

| `stop_reason` | 含義與處理 |
| --- | --- |
| `target_reached` | 文章與回應兩項額度皆達到；不是平台總量已收齊 |
| `search_exhausted` | PTT 已走完本次可讀取的指定看板搜尋路徑 |
| `input_exhausted` | Dcard 已處理本次提供的輸入；仍以來源狀態為準 |
| `post_limit` | 文章額度已用完，但回應尚未達到設定額度，停止探索其他文章 |
| `thread_comment_limit` | 至少一串回應因每串上限截斷，仍未達雙額度 |
| `comment_limit` | 回應總額度用完，但文章尚未達到設定額度，部分回應被截斷 |
| `page_limit` | 仍有下一頁，但本次搜尋頁數額度已用完 |
| `pagination_cycle` | PTT 翻頁回到已讀頁面，停止避免無限請求 |
| `article_parse_partial` | PTT 文章或搜尋頁無法完整辨識，或缺少有效文章日期 |
| `article_unavailable` | PTT 部分文章回傳 404／410 |
| `public_page_partial` | Dcard 只有摘要、缺少可確認的正文／留言，或有其他頁面解析缺漏 |
| `public_search_partial` | Dcard 公開搜尋頁無法確認已走完，或翻頁循環 |
| `unknown_date` | Dcard 項目缺少有效日期，未加入指定期間的分析資料 |
| `source_unavailable` | 部分資源不存在，或來源在有限重試後仍暫時無法連線；查看錯誤細節 |
| `public_source_unavailable` | Dcard 公開端點暫時失敗；仍處理已提供的匯入資料 |
| `blocked`／`public_source_blocked` | 來源要求登入、拒絕存取或回傳阻擋頁；Dcard 仍可處理已提供的匯入資料 |
| `canceled`／`application_shutdown` | 使用者取消或程式關閉，可利用已保存資料續抓 |

checkpoint 中的 `partial_reasons` 可包含多個同時發生的原因。搜尋頁數、文章請求數、
缺漏、重複與每串已收留言數用於診斷和續抓；它們不是平台對外公布的內容總量。
`PARTIAL` 不代表已保存的每筆內容都無效；可檢查證據後用現有資料分析，並在解讀報告時
保留來源覆蓋不足的限制。

## 升級與舊資料

這次補強沿用既有資料表與 JSON 設定，不新增 migration。一般安裝仍依 README 執行
`alembic upgrade head` 以確認原有版本遷移已完成；更新前照常備份資料庫和作者匿名金鑰。

新解析的論壇時間會先依臺灣時區解讀再儲存為 UTC；既有資料庫中的時間與歷史報告不會
自動重寫。需要以修正後日期重新觀察時，建立新任務蒐集，避免把新舊時間判定混為同一
份完整重算結果。

PTT 續抓支援舊版推文 ID 的相容比對，前提是原樓層與原始時間字串仍可從頁面重現。
如果舊推文刪除或樓層已位移，無法完整還原過往 ID；因此跨版本續抓不能保證所有歷史
情況都完全去重。Dcard 匯入主文統一使用 URL 的文章 ID，並保留舊版自動產生雜湊鍵的
相容比對；自行指定且不符合文章 URL 的舊資料需要重新整理後匯入。

## 公開網站查核紀錄

2026-09-07 的公開搜尋索引可找到官方頁面：

- [PTT Food 搜尋結果](https://www.ptt.cc/bbs/Food/search?page=2&q=author%3Afssmiling)：
  可確認 `/bbs/{board}/search?q=...`、`page` 與上／下頁導覽的存在。
- [Dcard 公開關鍵字搜尋](https://www.dcard.tw/search?query=McDonald) 與
  [指定 food 看板的搜尋](https://www.dcard.tw/search?forum=food&query=%E9%BA%A5%E7%95%B6%E5%8B%9E%E6%97%A9%E9%A4%90%E5%84%AA%E6%83%A0)：
  可確認 `query` 與 `forum` 的公開路徑形式。

本次直接開啟這些搜尋頁時，查核工具無法取得頁面；因此上述紀錄只確認公開索引與路徑，
不證明本程式已通過真站蒐集測試，也未確認 Dcard 目前提供固定頁碼式翻頁。
公開頁版型與可取得內容仍需使用者環境的少量 smoke test 驗證。

## 測試

預設測試使用離線 fixtures 與 HTTP mock，涵蓋解析、搜尋連結、去重、上限、續抓、
暫時失敗及存取阻擋，不需要真實帳號：

```powershell
uv run --no-sync pytest tests/test_forum_sources.py tests/test_ptt_collection.py tests/test_dcard_collection.py tests/test_forum_http.py tests/test_forum_config.py tests/test_dcard_imports.py
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync mypy src/simpsons_insight_agent
```

本次執行環境的非瀏覽器測試已驗證；Chromium 下載逾時，以下兩個瀏覽器測試檔尚未
完成驗證。重現相同的離線測試範圍可執行：

```powershell
uv run --no-sync pytest --ignore=tests/test_scraper_dom.py --ignore=tests/test_report_analytics_browser.py
```

完整 `pytest` 另需要可啟動的 Playwright 瀏覽器。以下兩項真站測試在本次驗證中保持停用。

真站 smoke test 預設停用。需要驗證連線時，可使用現有的一篇公開文章測試：

```powershell
$env:RUN_LIVE_PTT_TESTS = "1"
uv run --no-sync pytest tests/test_live_forums.py -k ptt

$env:RUN_LIVE_DCARD_URL_TESTS = "1"
$env:LIVE_DCARD_URL = "https://www.dcard.tw/f/food/p/替換成實際文章ID"
uv run --no-sync pytest tests/test_live_forums.py -k dcard
```

這兩項測試分別驗證 PTT 公開搜尋／文章及 Dcard 指定文章，不等同 Dcard 搜尋頁的實際
覆蓋率測試。遇到拒絕存取或年齡確認就停止，不使用代理池、帳號輪替或 CAPTCHA 繞過。
