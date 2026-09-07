let selectedCandidate = null;
let pendingPayload = null;
let currentJobId = null;
let eventSource = null;
let currentJob = null;
const dcardImportIds = [];

const byId = (id) => document.getElementById(id);
const show = (element) => element?.classList.remove("hidden");
const hide = (element) => element?.classList.add("hidden");
const splitValues = (value) => [...new Set(value.split(/[，,\n]/).map((item) => item.trim()).filter(Boolean))];

function currentSubjectTerms() {
  const customTerms = splitValues(byId("query")?.value || "");
  if (customTerms.length) return customTerms;
  return splitValues(`${byId("subject-name")?.value || ""},${byId("subject-aliases")?.value || ""}`);
}

function googleSearchQuery() {
  const customQuery = byId("query")?.value.trim();
  if (customQuery) return customQuery;
  return `${byId("subject-name")?.value || ""} ${byId("subject-address")?.value || ""}`.trim();
}

function refreshSearchScope() {
  const terms = currentSubjectTerms();
  const keywordText = terms.length ? terms.join("、") : "尚未輸入";
  const preview = byId("ptt-keyword-preview");
  if (preview) preview.textContent = keywordText;
  const scope = byId("search-scope");
  if (scope) scope.textContent = terms.length
    ? `目前搜尋詞：${keywordText}。Google Maps 會用它找候選分店；PTT 會在你選定的看板搜尋標題；Dcard 仍需指定公開文章。`
    : "請先輸入名稱，或在搜尋關鍵字欄輸入要查找的詞。地址只用於協助定位 Google Maps 分店。";
}

function isoDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

const today = new Date();
const yearAgo = new Date(today); yearAgo.setFullYear(today.getFullYear() - 1);
["ptt-date-to", "dcard-date-to"].forEach((id) => { if (byId(id)) byId(id).value = isoDate(today); });
["ptt-date-from", "dcard-date-from"].forEach((id) => { if (byId(id)) byId(id).value = isoDate(yearAgo); });
refreshSearchScope();

["query", "subject-name", "subject-address", "subject-aliases"].forEach((id) => {
  byId(id)?.addEventListener("input", refreshSearchScope);
});

document.querySelectorAll(".source-toggle input").forEach((checkbox) => {
  const card = checkbox.closest(".source-card");
  const sync = () => card.classList.toggle("enabled", checkbox.checked);
  checkbox.addEventListener("change", sync); sync();
});

byId("search-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = byId("search-status");
  const candidates = byId("candidates");
  candidates.innerHTML = "";
  status.classList.remove("error");
  const query = googleSearchQuery();
  if (query.length < 2) return alert("請先輸入主題名稱或搜尋關鍵字");
  status.textContent = "正在找 Google Maps 候選商家；其他可搜尋來源會在建立任務時沿用相同搜尋詞…"; show(status);
  try {
    const response = await fetch("/api/sources/google-maps/search", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({query, headless: false}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "搜尋失敗");
    status.textContent = data.candidates.length ? "請選擇正確的分店。" : "找不到候選商家。";
    data.candidates.forEach((candidate) => {
      const card = document.createElement("button");
      card.type = "button"; card.className = "candidate";
      card.innerHTML = `<strong>${escapeHtml(candidate.name)}</strong><span>${escapeHtml(candidate.address || "地址未辨識")}</span><small>${candidate.average_rating ?? "—"} ★ · ${candidate.total_review_count ?? "—"} 則評論</small>`;
      card.addEventListener("click", () => selectCandidate(candidate, card));
      candidates.appendChild(card);
    });
    if (data.direct_url && data.candidates.length === 1) selectCandidate(data.candidates[0], candidates.firstElementChild);
  } catch (error) {
    status.textContent = error.message; status.classList.add("error");
  }
});

function selectCandidate(candidate, card) {
  selectedCandidate = candidate;
  document.querySelectorAll(".candidate").forEach((item) => item.classList.remove("selected"));
  card?.classList.add("selected");
  byId("subject-name").value = candidate.name;
  byId("subject-address").value = candidate.address || "";
  byId("google-url").value = candidate.maps_url;
  byId("source-google").checked = true;
  byId("source-google").dispatchEvent(new Event("change"));
}

byId("upload-dcard")?.addEventListener("click", async () => {
  const file = byId("dcard-file").files[0];
  if (!file) return alert("請先選擇 CSV 或 JSON 檔案");
  const status = byId("dcard-import-status");
  status.textContent = "正在驗證匯入檔…";
  const form = new FormData(); form.append("file", file);
  const response = await fetch("/api/source-imports/dcard", {method: "POST", body: form});
  const data = await response.json();
  if (!response.ok) { status.textContent = data.detail || "匯入失敗"; status.classList.add("error"); return; }
  status.classList.remove("error");
  dcardImportIds.push(data.import_id);
  status.textContent = `已加入 ${data.filename}（${data.row_count} 筆）`;
  byId("source-dcard").checked = true;
  byId("source-dcard").dispatchEvent(new Event("change"));
});

function buildPayload() {
  const name = byId("subject-name").value.trim();
  if (!name) throw new Error("請輸入分析主題名稱");
  const aliases = splitValues(byId("subject-aliases").value);
  const keywords = currentSubjectTerms();
  const sources = [];
  if (byId("source-google").checked) {
    const mapsUrl = byId("google-url").value.trim();
    if (!mapsUrl) throw new Error("Google Maps 尚未指定分店；請先搜尋候選並點選，或直接貼上 Maps URL");
    sources.push({
      source: "google_maps", maps_url: mapsUrl, max_reviews: Number(byId("google-max").value),
      sort: byId("google-sort").value, headless: byId("headless").checked,
      average_rating: selectedCandidate?.average_rating ?? null,
      total_review_count: selectedCandidate?.total_review_count ?? null,
    });
  }
  if (byId("source-ptt").checked) {
    const boards = splitValues(byId("ptt-boards").value);
    if (!boards.length) throw new Error("PTT 來源至少需要一個看板");
    if (!keywords.length) throw new Error("請先輸入主題名稱或搜尋關鍵字，才能搜尋 PTT");
    sources.push({
      source: "ptt", boards, keywords,
      date_from: byId("ptt-date-from").value, date_to: byId("ptt-date-to").value,
      max_posts: Number(byId("ptt-max-posts").value), max_comments: Number(byId("ptt-max-comments").value),
      max_comments_per_thread: Number(byId("ptt-per-thread").value),
    });
  }
  if (byId("source-dcard").checked) {
    const urls = splitValues(byId("dcard-urls").value);
    if (!urls.length && !dcardImportIds.length) throw new Error("Dcard 目前不支援全站關鍵字搜尋；請貼上至少一個公開文章 URL 或加入 CSV／JSON 匯入檔");
    if (!byId("dcard-terms").checked) throw new Error("請確認 Dcard 來源條款提醒");
    sources.push({
      source: "dcard", urls, import_ids: dcardImportIds,
      date_from: byId("dcard-date-from").value, date_to: byId("dcard-date-to").value,
      max_posts: Number(byId("dcard-max-posts").value), max_comments: Number(byId("dcard-max-comments").value),
      max_comments_per_thread: Number(byId("dcard-per-thread").value), acknowledge_terms: true,
    });
  }
  if (!sources.length) throw new Error("請至少選擇一個資料來源");
  return {
    subject: {kind: byId("subject-kind").value, name, address: byId("subject-address").value.trim() || null, aliases},
    sources, llm_model: byId("llm-model").value,
    auto_plan: byId("auto-plan").checked,
    planning_options: {start_date: byId("planning-start").value || null, weekly_hours: byId("planning-hours").value ? Number(byId("planning-hours").value) : null, constraints: byId("planning-constraints").value},
  };
}

byId("job-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  try { pendingPayload = buildPayload(); }
  catch (error) { return alert(error.message); }
  byId("preview-content").innerHTML = `
    <article><span>分析主題</span><strong>${escapeHtml(pendingPayload.subject.name)}</strong><small>${escapeHtml(pendingPayload.subject.address || "未指定地址")}</small></article>
    <article><span>共用搜尋詞</span><strong>${escapeHtml(currentSubjectTerms().join("、") || "未指定")}</strong><small>Google Maps 與 PTT 會沿用；Dcard 需指定公開文章</small></article>
    ${pendingPayload.sources.map((source) => `<article><span>來源</span><strong>${sourceLabel(source.source)}</strong><small>${sourceSummary(source)}</small></article>`).join("")}`;
  show(byId("job-preview")); byId("job-preview").scrollIntoView({behavior: "smooth"});
});

byId("edit-preview")?.addEventListener("click", () => { hide(byId("job-preview")); byId("job-form").scrollIntoView({behavior: "smooth"}); });
byId("create-job")?.addEventListener("click", async () => {
  if (!pendingPayload) return;
  const response = await fetch("/api/jobs", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(pendingPayload)});
  const data = await response.json();
  if (!response.ok) return alert(data.detail || "建立任務失敗");
  currentJobId = data.id; currentJob = data; hide(byId("job-preview")); show(byId("job-progress"));
  byId("job-progress").scrollIntoView({behavior: "smooth"}); renderJob(data); watchJob(data.id);
});

function watchJob(jobId) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/jobs/${jobId}/events`);
  eventSource.onmessage = (event) => renderJob(JSON.parse(event.data));
  eventSource.onerror = () => eventSource.close();
}

function renderJob(job) {
  currentJob = job;
  const percent = Math.round(job.progress * 100);
  byId("job-state").textContent = job.status;
  byId("progress-bar").style.width = `${percent}%`;
  byId("progress-bar").parentElement?.setAttribute("aria-valuenow", String(percent));
  byId("progress-percent").textContent = `${percent}%`;
  byId("job-counts").textContent = `${job.collected_count} / ${job.max_reviews}`;
  byId("job-message").textContent = job.message || "";
  if (job.error) { byId("job-error").textContent = job.error; show(byId("job-error")); } else hide(byId("job-error"));
  const sourceProgress = byId("source-progress"); sourceProgress.innerHTML = "";
  (job.sources || []).forEach((source) => {
    const item = document.createElement("article"); item.className = "source-progress-row";
    const error = source.error?.replace(`${source.stop_reason}: `, "");
    const detail = [source.stop_reason, error].filter(Boolean).join(" · ");
    item.innerHTML = `<strong>${sourceLabel(source.source)}</strong><span class="badge status-${source.status.toLowerCase()}">${escapeHtml(source.status)}</span><span>${source.collected_count} 筆${source.post_count ? ` · ${source.post_count} 篇` : ""}${source.comment_count ? ` · ${source.comment_count} 則回應` : ""}</span><small>${escapeHtml(detail)}</small>`;
    sourceProgress.appendChild(item);
  });
  byId("resume-job").textContent = job.status === "WAITING_FOR_USER" ? "人工驗證完成，繼續" : "繼續蒐集";
  (job.status === "WAITING_FOR_USER" || job.can_resume_collection) ? show(byId("resume-job")) : hide(byId("resume-job"));
  job.can_start_analysis ? show(byId("analyze-job")) : hide(byId("analyze-job"));
  const idle = ["READY_FOR_ANALYSIS", "COLLECTION_INTERRUPTED", "COMPLETED", "PARTIAL", "FAILED", "BLOCKED", "CANCELED"];
  idle.includes(job.status) ? hide(byId("cancel-job")) : show(byId("cancel-job"));
  show(byId("delete-job"));
  if (job.report_id) { byId("open-report").href = `/reports/${job.report_id}`; show(byId("open-report")); } else hide(byId("open-report"));
}

byId("resume-job")?.addEventListener("click", async () => {
  if (!currentJobId) return;
  const response = await fetch(`/api/jobs/${currentJobId}/resume`, {method: "POST"}); const data = await response.json();
  if (!response.ok) return alert(data.detail || "無法續跑"); renderJob(data); watchJob(currentJobId);
});
byId("cancel-job")?.addEventListener("click", async () => { if (currentJobId) await fetch(`/api/jobs/${currentJobId}/cancel`, {method: "POST"}); });

async function deleteTask(jobId, label = "這個任務", onSuccess = null) {
  if (!confirm(`確定刪除「${label}」？任務、報告、分析結果與該任務獨有的爬文資料都會移除。`)) return false;
  const response = await fetch(`/api/jobs/${jobId}`, {method: "DELETE"});
  if (!response.ok) {
    let detail = "刪除任務失敗";
    try { detail = (await response.json()).detail || detail; } catch (_error) { /* empty response */ }
    alert(detail);
    return false;
  }
  if (eventSource) eventSource.close();
  onSuccess?.();
  return true;
}

byId("delete-job")?.addEventListener("click", async () => {
  if (!currentJobId) return;
  const name = currentJob?.message || "目前任務";
  if (await deleteTask(currentJobId, name)) window.location.href = "/";
});

document.querySelectorAll(".delete-existing").forEach((button) => button.addEventListener("click", async () => {
  button.disabled = true;
  const row = button.closest("tr");
  const deleted = await deleteTask(button.dataset.jobId, button.dataset.jobName || "這個任務", () => row?.remove());
  if (!deleted) button.disabled = false;
}));

async function startAnalysis(jobId, collectionComplete) {
  const acceptPartial = collectionComplete || confirm("部分來源尚未完整。要以目前蒐集到的內容建立報告嗎？");
  if (!acceptPartial) return;
  const response = await fetch(`/api/jobs/${jobId}/analyze`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({llm_model: byId("llm-model").value, accept_partial_collection: !collectionComplete})});
  const data = await response.json(); if (!response.ok) return alert(data.detail || "無法開始分析");
  currentJobId = jobId; show(byId("job-progress")); renderJob(data); watchJob(jobId);
}
byId("analyze-job")?.addEventListener("click", async () => { if (currentJobId && currentJob) await startAnalysis(currentJobId, currentJob.collection_complete); });
document.querySelectorAll(".resume-existing").forEach((button) => button.addEventListener("click", async () => {
  currentJobId = button.dataset.jobId; const response = await fetch(`/api/jobs/${currentJobId}/resume`, {method: "POST"}); const data = await response.json();
  if (!response.ok) return alert(data.detail || "無法續跑"); show(byId("job-progress")); renderJob(data); watchJob(currentJobId);
}));
document.querySelectorAll(".analyze-existing").forEach((button) => button.addEventListener("click", async () => startAnalysis(button.dataset.jobId, button.dataset.complete === "true")));

function sourceLabel(source) { return ({google_maps: "Google Maps", ptt: "PTT", dcard: "Dcard"})[source] || source; }
function sourceSummary(source) {
  if (source.source === "google_maps") return `最多 ${source.max_reviews} 則評論`;
  if (source.source === "ptt") return `${source.boards.join("、")} · 自動搜尋 ${source.keywords.join("、")} · ${source.max_posts} 篇／${source.max_comments} 則推文`;
  return `${source.urls.length} 篇公開文章、${source.import_ids.length} 個匯入檔 · ${source.max_posts} 篇／${source.max_comments} 則留言`;
}
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]); }
