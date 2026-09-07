# 第二輪：議題與品牌比較操作文件

2026-09-07。N1～N5 的程式實作與介面如下。研究來源仍以 [OpView 截圖紀錄](opview-ui-reference.md) 為準；以下是本系統的設計與行為，並非 OpView 未公開公式。

## 操作流程

1. 報告先顯示營運分析；原完整摘要與舊版逐筆內容在折疊區。套用共用條件後，查看文字情緒／來源的筆數或比例、聲量、負評比例與 P/N 趨勢。負評比例採百分比座標，與 P/N 分開。
2. 來源／頻道排行可選分組、樣本數／負面筆數／負評比例排序，切換橫條圖與表格。比例分母是已分類文字，完整表格保留分母，缺值排最後。
3. 熱門內容可切換內容明細與討論串。明細按日期新舊排序；討論串按已蒐集數、平台回應數或最近日期排序。缺日期與缺互動資料標為未提供，並排最後；同值以來源及識別碼固定順序。每頁25筆，API上限100筆。
4. 在「自訂議題比較」新增1～5張主題卡，輸入名稱及任一詞、必須詞、排除詞。可以先保存草稿，選取2～5張再比較。
5. 保存時採用報告**已套用**的條件，不採用尚在編輯的欄位。由已保存清單，或比較頁的「返回報告編輯主題卡」重開，可恢復主題設定及共用條件。
6. 比較頁呈現總量、正中負、未知／僅星等、負評比例、P/N、共同期間趨勢、來源組成、前20頻道與前25熱門討論串。點圖表或對象開啟證據抽屜，再連至原報告既有改善工作。
7. 品牌比較選2～5個不同品牌報告，保存後可修改名稱、期間、来源、粒度、情緒、內容類型、面向及文字。原報告不改寫；部分引用報告刪除時，列出缺失識別碼並保留其餘結果。
8. 共用 SVG 圖表可下載 PNG／JPEG。圖例控制可見序列；完整數值表與比較 CSV 仍保留全部序列。比較列印頁使用瀏覽器另存 PDF。

## 查詢及計算契約

- 先以共用 `ReportFilters` 篩選母資料，再逐一套用主題任一／必須／排除詞；两個任一詞組是交集，不直接合併為更寬鬆的 OR。
- 名稱只作標籤。主題設定不蒐集新內容、不重新分類、不啟動改善計畫。
- 各主題樣本可以重疊；聯集是至少命中一個選取主題的不同內容數，重疊是同時命中至少兩個選取主題的不同內容數。兩者皆按報告＋內容ID去重。
- 情緒分母是正／中／負已分類文字；來源比例分母是納入樣本。未知與僅星等另列。負評比例及 P/N 零分母回空值；不把缺資料當作零。
- 比較使用相同日期桶與台北時區精度規則。下鑽先保留品牌／議題母集合，再與日期、情緒、來源等條件取交集，不能放寬原比較。
- 跨品牌不接受共用語意 `topic_key`；同名稱分群不推定相同議題。共同負面面向使用既有面向統計，不是市占率。
- 來源及頻道不從 URL 猜測，沿用 V4 快照欄位。V3 缺值保留，無快照舊報告仍標 `legacy_live`。
- 比較設定沿用 `BrandComparison.config`，新設定 `config_version=2`、`mode=topics|brands`；舊設定省略 mode 時視為 brands。沒有新資料表或 migration。

## API

| 方法／介面 | 契約 |
| --- | --- |
| `GET /api/reports/{id}/channels` | `group_by=channel|source`，預設channel；`sort=sample_count|negative_count|negative_ratio`，預設sample_count |
| `GET /api/reports/{id}/items`、`/reviews` | `sort=original|date_desc|date_asc`，保留原始預設及分頁 |
| `GET /api/reports/{id}/threads` | `sort=collected_count|reported_reply_count|latest_date`；增加latest_date、date_precision、excerpt、board與source_url |
| `POST /api/reports/{id}/topic-comparisons` | `{name, mode: "topics", filters, topics, selected_ids}`；回201及id |
| `GET /api/reports/{id}/topic-comparisons` | 同報告已保存議題設定清單 |
| `POST /api/comparisons` | 原品牌建立介面；新增共用篩選，保留既有欄位 |
| `PATCH /api/comparisons/{id}` | 完整設定更新；不可變更模式或議題所屬報告，非法設定回422 |
| `GET /api/comparisons/{id}` | 原brands保留，增加groups、config、mode、ready、missing_report_ids、filters及聯集／重疊數 |
| `GET /api/comparisons/{id}/members/{member}/items` | brands的member為報告ID；topics為卡片ID。支援共用篩選與分頁，回母集合交集、scope及report_id；`format=csv`匯出全部符合明細 |
| `GET /api/comparisons/{id}/export?format=csv` | UTF-8 BOM長表：comparison、member、report_id、period、metric、value、scope_key；全部序列，議題另含聯集／重疊列 |
| `GET /comparisons/{id}/print` | 本機列印頁，A4直式、15mm邊界，條件、排行、圖表、缺值／來源限制與議題重疊說明 |

圖檔由瀏覽器 Canvas 將本機 SVG 輸出兩倍像素，含條件、期間、圖例與限制；JPEG白底。不使用外部服務、字型或圖片。CSV文字使用既有公式開頭轉義，空比例保留空欄位。列印僅呈現重點排行，完整趨勢由CSV取得。

## 維護與驗收

完整測試結果：**94 passed、2 skipped**；Ruff（src、tests）通過，Mypy全部27個來源檔通過。Chrome實際下載的圖檔中文及圖例可讀；比較PDF四頁已逐頁轉圖檢查，操作列隱藏，圖表未切斷，表格未溢出。Mypy使用暫存快取目錄，避開既有快取寫入限制。

- `comparisons.py` 集中解析比較母集合、重疊與統計；`comparison_api.py` 負責設定、證據、CSV與列印。
- `report-components.js` 共用圖表、數值表、圖檔下載與證據抽屜；`analytics.js`、`comparison-workspace.js` 共用它，不在前端重算後端統計。
- `test_comparison_workspace.py` 驗證主題交集、重疊／非重疊、草稿、編輯移除、非法選取、缺值排序、跨來源同名頻道、舊比較及已刪除引用。
- `test_report_analytics_browser.py` 使用獨立SQLite、合成餐飲資料與無模型替身，驗證快速套用、鍵盤與焦點返回、品牌更新、議題保存重開、重疊下鑽、PNG／JPEG下載、比較CSV及PDF產出。
- 既有 `test_report_analytics.py` 繼續涵蓋快照凍結、V3缺欄位、日週月精度、零分母、空期間及匯出一致性。

本輪範圍不含公開分享、XLSX、人口推估、新來源、持續監測與預警；原有改善計畫生成仍依既有模型設定運作。基本篩選、比較與交付不需要付費模型。
