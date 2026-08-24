const reportId = document.body.dataset.reportId;
let page = 1;
let sessionId = null;
const pageSize = 25;
const byId = (id) => document.getElementById(id);

async function loadReviews(reviewId = null) {
  const params = new URLSearchParams({page, page_size: pageSize});
  if (reviewId) params.set("review_id", reviewId);
  else {
    if (byId("filter-q").value) params.set("q", byId("filter-q").value);
    if (byId("filter-source").value) params.set("source", byId("filter-source").value);
    if (byId("filter-content-type").value) params.set("content_type", byId("filter-content-type").value);
    if (byId("filter-sentiment").value) params.set("sentiment", byId("filter-sentiment").value);
    if (byId("filter-rating").value) params.set("rating", byId("filter-rating").value);
    if (byId("filter-aspect").value) params.set("aspect", byId("filter-aspect").value);
  }
  const response = await fetch(`/api/reports/${reportId}/items?${params}`);
  const data = await response.json();
  const container = byId("reviews");
  container.innerHTML = "";
  data.items.forEach((review) => {
    const item = document.createElement("article");
    item.className = `review-card ${review.sentiment ? `review-${review.sentiment}` : ""}`;
    item.id = `review-${review.id}`;
    const source = sourceLabel(review.source);
    const contentType = contentTypeLabel(review.content_type);
    const title = review.title ? escapeHtml(review.title) : contentType;
    const titleMarkup = review.source_url
      ? `<a href="${escapeHtml(review.source_url)}" target="_blank" rel="noopener noreferrer">${title}</a>`
      : `<strong>${title}</strong>`;
    const forum = review.board ? `<span>${escapeHtml(review.board)}</span>` : "";
    const rating = review.source === "google_maps" && review.rating
      ? `<span class="stars" aria-label="${review.rating} 星">${"★".repeat(review.rating)}${"☆".repeat(5 - review.rating)}</span>`
      : "";
    const reactionCount = review.platform_data?.reaction_count;
    const heat = Number.isFinite(reactionCount) ? `<span>互動 ${reactionCount}</span>` : "";
    item.innerHTML = `
      <div class="item-context"><span class="badge source-${escapeHtml(review.source)}">${source}</span><span>${contentType}</span>${forum}${heat}${titleMarkup}</div>
      <div class="review-head">${rating}<span class="badge sentiment-${escapeHtml(review.sentiment || "unknown")}">${label(review.sentiment)}</span><time>${escapeHtml(review.relative_date || "日期未知")}</time></div>
      <p>${escapeHtml(review.text || "（只有星等，沒有文字）")}</p>
      <div class="tags">${review.aspects.map((aspect) => `<span>${aspectLabel(aspect)}</span>`).join("")}${review.rating_text_conflict ? "<span class='conflict'>星等／文字不一致</span>" : ""}</div>
      ${review.key_points.length ? `<ul>${review.key_points.map((point) => `<li>${escapeHtml(point)}</li>`).join("")}</ul>` : ""}
      ${review.owner_reply ? `<details><summary>商家回覆</summary><p>${escapeHtml(review.owner_reply)}</p></details>` : ""}`;
    container.appendChild(item);
  });
  byId("page-info").textContent = reviewId ? "證據評論" : `第 ${data.page} 頁 · 共 ${data.total} 則`;
  byId("prev-page").disabled = page <= 1 || Boolean(reviewId);
  byId("next-page").disabled = page * pageSize >= data.total || Boolean(reviewId);
}

byId("review-filters")?.addEventListener("submit", (event) => { event.preventDefault(); page = 1; loadReviews(); });
byId("source-tabs")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-source]");
  if (!button) return;
  byId("filter-source").value = button.dataset.source;
  document.querySelectorAll("#source-tabs button").forEach((tab) => {
    const active = tab === button;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  page = 1;
  loadReviews();
});
byId("filter-source")?.addEventListener("change", () => {
  document.querySelectorAll("#source-tabs button").forEach((tab) => {
    const active = tab.dataset.source === byId("filter-source").value;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
});
byId("sentiment-tabs")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-sentiment]");
  if (!button) return;
  byId("filter-sentiment").value = button.dataset.sentiment;
  document.querySelectorAll("#sentiment-tabs button").forEach((tab) => tab.classList.toggle("active", tab === button));
  page = 1;
  loadReviews();
});
byId("filter-sentiment")?.addEventListener("change", () => {
  document.querySelectorAll("#sentiment-tabs button").forEach((tab) => tab.classList.toggle("active", tab.dataset.sentiment === byId("filter-sentiment").value));
});
byId("report-tabs")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-report-source]");
  if (!button) return;
  document.querySelectorAll("#report-tabs button").forEach((tab) => {
    const active = tab === button;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll("[data-report-panel]").forEach((panel) => { panel.hidden = panel.dataset.reportPanel !== button.dataset.reportSource; });
});
byId("prev-page")?.addEventListener("click", () => { if (page > 1) { page--; loadReviews(); } });
byId("next-page")?.addEventListener("click", () => { page++; loadReviews(); });

byId("delete-report-job")?.addEventListener("click", async () => {
  if (!confirm("確定刪除這個任務？任務、報告、分析結果與爬文資料都會移除。")) return;
  const response = await fetch(`/api/jobs/${document.body.dataset.jobId}`, {method: "DELETE"});
  if (!response.ok) {
    let detail = "刪除任務失敗";
    try { detail = (await response.json()).detail || detail; } catch (_error) { /* empty response */ }
    alert(detail);
    return;
  }
  window.location.href = "/";
});

byId("question-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = byId("question").value.trim();
  if (!question) return;
  appendMessage("user", question);
  byId("question").value = "";
  const response = await fetch(`/api/reports/${reportId}/questions`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({question, session_id: sessionId, llm_model: byId("question-model").value}),
  });
  const data = await response.json();
  if (!response.ok) return appendMessage("assistant", data.detail || "問答失敗");
  sessionId = data.session_id;
  const evidence = data.evidence_review_ids.map((id) => `<button class="evidence" data-id="${id}">${id.slice(0, 8)}</button>`).join(" ");
  const limitations = data.limitations.length ? `<small>限制：${data.limitations.map(escapeHtml).join("；")}</small>` : "";
  appendMessage("assistant", `${escapeHtml(data.answer)}<div class="evidence-list">${evidence}</div>${limitations}`, true);
});

byId("chat")?.addEventListener("click", (event) => {
  const button = event.target.closest("button.evidence");
  if (button) { page = 1; loadReviews(button.dataset.id).then(() => byId("reviews").scrollIntoView({behavior: "smooth"})); }
});

function appendMessage(role, content, html = false) {
  const item = document.createElement("div"); item.className = `message ${role}`;
  if (html) item.innerHTML = content; else item.textContent = content;
  byId("chat").appendChild(item); item.scrollIntoView({behavior: "smooth"});
}
function label(value) { return ({positive:"正面", neutral:"中立", negative:"負面", rating_only:"僅星等"})[value] || "待分析"; }
function sourceLabel(value) { return ({google_maps:"Google Maps", ptt:"PTT", dcard:"Dcard"})[value] || value; }
function contentTypeLabel(value) { return ({review:"評論", post:"文章", comment:"留言／推文"})[value] || value; }
function aspectLabel(value) { return ({product_quality:"產品／品質",service:"服務",price_value:"價格／價值",environment:"環境",speed_wait:"效率／等候",convenience_accessibility:"便利／可及性",brand_reputation:"品牌聲譽",marketing_communication:"行銷／溝通",workplace:"職場／雇主",trust_safety:"信任／安全",other:"其他"})[value] || value; }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char])); }

loadReviews();
