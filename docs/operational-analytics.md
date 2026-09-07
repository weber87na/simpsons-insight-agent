# 店家／品牌營運分析使用與實作紀錄

2026-09-07：M1～M5 已實作；第二輪 N1～N5 的主題卡、完整比較、排序與圖檔交付見[議題與品牌比較文件](comparison-workspace.md)。研究依據見 [OpView 截圖](opview-ui-reference.md)；原始設計草案保留供追溯。

## 開始使用

執行 `uv sync --no-editable` 更新應用程式與固定版本 `jieba==0.42.1`，再依 README 啟動。此補強沒有新增 migration；既有決策功能仍需要原來的 `0005_decision`。本次測試使用獨立暫存 SQLite，未修改營運資料庫。

1. 開啟報告，主要區域即為營運分析。完整管理摘要及舊明細移至折疊區；它們仍代表原有報告範圍，營運區提供共用條件與下鑽。
2. 選日期、日／週／月、來源、情緒、內容類型、面向或主題。文字詞組以逗號、頓號或換行分隔，按「套用條件」。尚未套用時圖表與匯出維持上一組條件。
3. 查看樣本、P/N、完整文字情緒序列及來源圖。平均線包含空期間；圖例只控制可見序列，不改變匯出範圍。
4. 點日期點、情緒摘要或來源／頻道排行，開啟證據抽屜。使用鍵盤 Tab、Enter／Space 也可操作；Escape 關閉並回到原焦點。
5. 在討論串中切頁，或從證據連回已存在的改善工作。平台回應數與已蒐集筆數分開；沒有平台數據時顯示未提供，不當作零。
6. 切換文字探索的詞頻／文件頻率，點詞回查。詞語情緒選擇會在該區取代全域情緒條件，區段與抽屜會標示實際範圍。
7. 使用同條件明細 CSV／JSON、趨勢 CSV，或開啟列印頁另存 PDF。抽屜內的匯出只包含該次下鑽條件。

品牌比較頁可選 2～5 個不同品牌報告，使用共同日期、來源及日／週／月粒度，呈現同步聲量、負評比例與共同負面面向。樣本組成差異仍提示，不推論市占率。

## 資料與公式

- 新建立報告保存 V4 快照：遮罩正文／標題／回覆、日期與精度、來源與頻道、討論串、安全原文 URL、星等與分析欄位。平台資料只保存白名單數值以及 PTT 推／噓／箭頭、樓層，不保存作者。
- 已存在快照，包括空快照，不會因重試而重建。V3 缺欄位不回填；無快照舊報告標為 `legacy_live`，僅這類報告可能受後續資料更新影響。
- 所有新增分析使用共用 `ReportFilters`；相同正規化条件、版本及內容得到同一 `scope_key`。它是對帳識別，不是分享權限。
- 負評比例＝負面／正中負已分類文字；P/N＝正面／負面。無負面或無分類回 null，附原因；未知與僅星等只進樣本總數。
- 日期使用 Asia/Taipei；日接受 day，週接受 day/week，月接受 day/week/month。未知與精度不足排除數分開顯示。圖表、抽屜及趨勢匯出強制沿用粒度精度規則；一般明細 API 可使用 `precision_policy=all`。
- 任一詞組內 OR，必須詞全數符合，排除任一命中即排除，組間 AND。新增詞組比對遮罩標題＋正文，NFKC/casefold，不做繁簡轉換或正則執行；原 `q` 保留字面正文搜尋。
- 關鍵詞採 jieba 0.42.1 內建詞典＋版本化繁體餐飲補充詞表，HMM 關閉，固定停用詞；品牌與別名加入字典。詞頻是 token 次數，文件頻率是不同內容 ID 數。排行與固定排列文字雲共用結果；未知詞可能被排除，並非完整語意分析。

## API

報告前綴為 `/api/reports/{report_id}`。共用參數包括 `date_from`、`date_to`、`interval`、`precision_policy`、`source`、`content_type`、`sentiment`、`aspect`、`topic_key`、`board`、`channel_label`、`thread_source_id`、`review_id`、`rating`、`q`、`keyword`，以及可重複的 `any_terms`／`all_terms`／`exclude_terms`。每組最多20詞，每詞1～100字，非法條件回422。

| 介面 | 回應／用途 |
| --- | --- |
| `GET /summary` | 樣本、分類、P/N、來源及討論串統計＋scope |
| `GET /trends` | 原有 points／計數保留，加日粒度、情緒、P/N、來源、period_end、平均值及scope |
| `GET /items`、`/reviews` | 原有欄位與分頁保留，加入scope與主題鍵；25筆／頁，上限100 |
| `GET /channels` | 來源／頻道排行，缺頻道使用明確標籤並可精確下鑽 |
| `GET /threads` | 討論串分頁；sort=collected_count、reported_reply_count 或 latest_date，缺值排最後 |
| `GET /keywords` | metric=term_frequency 或 document_frequency；limit預設30、上限100；show_brand預設true |
| `GET /evidence/{item_id}` | 保留同報告單筆證據路由 |
| `GET /export?format=csv|json` | 同篩選未分頁明細；JSON items／reviews相同，包含scope |
| `GET /trends/export?format=csv` | 每桶數值、空比例及scope_key，UTF-8 BOM |
| `GET /reports/{id}/print` | 同條件列印頁、來源限制、圖表、前十關鍵詞及既有改善工作 |
| `POST /api/comparisons` | 新增interval，舊設定預設month；讀取比較重用共用精度與計算 |

來源 URL 限 http(s)。CSV 文字公式開頭會加單引號，避免試算表執行；JSON 不添加試算表轉義。文字探索與基本統計不呼叫付費模型。

## 驗證與限制

本輪驗證結果：完整 pytest **91 passed、2 skipped**；ruff（src、tests）通過；mypy 全部25個來源檔通過。略過項目沿用原測試套件的執行條件；新增營運分析API及Chrome整合測試均有實際執行。

新增 `test_report_analytics.py` 驗證條件組合、日期精度、零分母、快照凍結、舊報告、CSV／JSON／趨勢／列印對帳與品牌比較。`test_report_analytics_browser.py` 使用獨立資料庫、無模型的改善工作替身及全新 headless Chrome，驗證連續套用、DOM ID 唯一、日期鍵盤下鑽、25筆分頁、關鍵詞、改善工作跳轉、比較圖表與PDF產出。需本機安裝 Chrome；既有其他瀏覽器測試需求依README。

```powershell
uv run pytest -q
uv run ruff check src tests
uv run mypy src/simpsons_insight_agent
```

實際輸出的兩頁 A4 PDF 已轉成圖片檢查，條件、圖表、表格與改善工作可讀，圖表未跨頁。沒有新增公開分享、資料來源、人口推估、市占矩陣、監測或通知；既有決策產生按鈕仍依原設定運作，不會因篩選、列印或下鑽自動啟動。
