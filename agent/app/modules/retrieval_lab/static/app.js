/* NewsAgent local annotation client. External news is rendered only as text. */
(function (root) {
  "use strict";
  const LABELS = Object.freeze({ relevant: "相关", partial: "部分相关", irrelevant: "不相关", unsure: "无法判断" });
  const TIERS = Object.freeze({ hot: "热层", warm: "温层", cold: "冷层" });
  function safeSourceUrl(value) {
    try {
      const url = new URL(String(value));
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }
  function finiteNumber(value) { return typeof value === "number" && Number.isFinite(value) ? value : null; }
  function metricText(value, percent) {
    const number = finiteNumber(value);
    return number === null ? "待标注" : percent ? (number * 100).toFixed(1) + "%" : number.toFixed(3);
  }
  function countsFrom(candidates) {
    const list = Array.isArray(candidates) ? candidates : [];
    let judged = 0, unable = 0;
    list.forEach(function (item) {
      const label = item.judgment && item.judgment.label;
      if (label === "unsure") unable += 1;
      else if (Object.prototype.hasOwnProperty.call(LABELS, label)) judged += 1;
    });
    return { total: list.length, judged: judged, unable: unable, touched: judged + unable };
  }
  function hasJudgment(candidate) {
    return Boolean(candidate && candidate.judgment && Object.prototype.hasOwnProperty.call(LABELS, candidate.judgment.label));
  }
  function buildJudgment(candidate, label, notes, reviewer) {
    if (!Object.prototype.hasOwnProperty.call(LABELS, label)) throw new Error("请先选择一个相关性标签。");
    const identity = String(reviewer || "").trim();
    if (!identity) throw new Error("请填写标注人。");
    const revision = candidate.judgment ? candidate.judgment.revision : candidate.revision;
    if (revision != null && (!Number.isSafeInteger(revision) || revision < 0)) throw new Error("标注版本无效，请重新加载当前查询。");
    return { label: label, notes: String(notes || "").trim(), reviewer: identity, expected_revision: revision == null ? 0 : revision };
  }
  const helpers = { LABELS: LABELS, safeSourceUrl: safeSourceUrl, metricText: metricText, countsFrom: countsFrom, hasJudgment: hasJudgment, buildJudgment: buildJudgment };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  root.NewsAnnotationHelpers = helpers;
  if (typeof document === "undefined") return;

  const state = { queries: [], run: null, index: 0, dirty: false, saving: false, loading: false, requestId: 0 };
  const $ = function (id) { return document.getElementById(id); };
  const text = function (id, value) { $(id).textContent = value == null ? "" : String(value); };
  const clear = function (element) { element.replaceChildren(); };
  function make(tag, value, className) {
    const element = document.createElement(tag);
    if (value != null) element.textContent = String(value);
    if (className) element.className = className;
    return element;
  }
  function errorMessage(error) { return error && error.message ? error.message : "请求失败，请重试。"; }
  function displayError(message) { text("global-error", message || ""); $("global-error").hidden = !message; }
  async function api(path, options) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json", "X-Lab-Request": "1" }, cache: "no-store" }, options || {}));
    let payload;
    try { payload = await response.json(); } catch (_) { throw new Error("服务没有返回有效数据，请确认本地服务仍在运行。"); }
    if (!response.ok) {
      const detail = payload.detail;
      const message = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : "请求失败（" + response.status + "）";
      const error = new Error(message); error.status = response.status; throw error;
    }
    return payload;
  }
  function current() { return state.run && state.run.candidates[state.index]; }
  function allowLeave() {
    if (state.saving) return false;
    return !state.dirty || window.confirm("当前判断尚未保存。离开后会丢弃这次修改，是否继续？");
  }
  function setBusy(value) {
    state.loading = value;
    $("loading-state").hidden = !value;
    $("run-content").hidden = value || !state.run;
    $("search-button").disabled = value || state.saving;
    $("refresh-queries").disabled = value || state.saving;
  }
  function updateSaveButtons() {
    const unavailable = state.saving || state.loading || !current();
    $("save").disabled = unavailable;
    $("save-next").disabled = unavailable;
    $("save").textContent = state.saving ? "正在保存…" : "保存当前判断";
    $("save-next").textContent = state.saving ? "正在保存…" : state.run && state.index === state.run.candidates.length - 1 ? "保存并完成此查询" : "保存并下一篇";
    $("label-options").disabled = unavailable;
    $("judgment-notes").disabled = unavailable;
    $("reviewer").disabled = unavailable;
    $("previous-candidate").disabled = unavailable || state.index === 0;
    $("next-candidate").disabled = unavailable || state.index >= state.run.candidates.length - 1;
    const queryIndex = state.queries.findIndex(function (q) { return state.run && q.run_id === state.run.run_id; });
    $("previous-query").disabled = state.saving || state.loading || queryIndex <= 0;
    $("next-query").disabled = state.saving || state.loading || queryIndex < 0 || queryIndex >= state.queries.length - 1;
    $("search-button").disabled = state.saving || state.loading;
  }
  function progressText(progress, fallback) {
    if (fallback) return fallback.touched + " / " + fallback.total + " 已处理 · " + fallback.judged + " 已判断" + (fallback.unable ? " · " + fallback.unable + " 无法判断" : "");
    if (!progress) return "待开始";
    const total = progress.total ?? progress.total_count ?? progress.candidate_count;
    const judged = progress.judged ?? progress.judged_count ?? progress.labeled_count ?? 0;
    const unable = progress.unable ?? progress.unable_count ?? progress.unsure_count ?? 0;
    return total == null ? "待开始" : (judged + unable) + " / " + total + " 已处理";
  }
  function renderQueries() {
    const list = $("query-list"); clear(list);
    const filter = $("query-filter").value.trim().toLowerCase();
    const queries = state.queries.filter(function (q) { return (String(q.text || "") + " " + String(q.category || "")).toLowerCase().includes(filter); });
    text("query-count", "共 " + state.queries.length + " 个查询" + (filter ? " · 匹配 " + queries.length + " 个" : ""));
    if (!queries.length) list.appendChild(make("p", filter ? "没有匹配的查询。" : "可在下方添加自己的查询。", "muted"));
    queries.forEach(function (query) {
      const button = make("button", null, "query-item"); button.type = "button";
      const active = Boolean(state.run && query.run_id === state.run.run_id);
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "true");
      button.append(make("strong", query.text), make("small", (query.category || "全部板块") + " · " + progressText(query.progress)));
      button.addEventListener("click", function () { if (allowLeave()) openRun(query.run_id); });
      list.appendChild(button);
    });
  }
  async function refreshQueries() {
    const payload = await api("/api/queries");
    state.queries = Array.isArray(payload.items) ? payload.items : [];
    renderQueries();
  }
  function syncCurrentQuery() {
    if (!state.run) return;
    const query = state.queries.find(function (item) { return item.run_id === state.run.run_id; });
    if (query) query.progress = state.run.progress;
    renderQueries();
  }
  function renderStatus(status) {
    const corpus = status.corpus || {};
    const model = status.model || {};
    const count = corpus.document_count ?? corpus.article_count ?? corpus.news_count ?? status.document_count;
    const modelName = typeof model === "string" ? model : model.name || model.model_name || model.model_id || "见下方版本记录";
    const note = (count == null ? "年度新闻语料" : count + " 篇来源新闻") + " · 模型：" + modelName + "。冷热温分层按发布日期年龄进行实验分配，不表示真实用户热度。";
    text("environment-note", note);
    const warnings = $("environment-warnings"); clear(warnings);
    (Array.isArray(status.warnings) ? status.warnings : []).forEach(function (warning) { warnings.appendChild(make("li", typeof warning === "string" ? warning : JSON.stringify(warning))); });
    text("environment-json", JSON.stringify(status, null, 2));
  }
  function tierCard(label, value) {
    const card = make("div", null, "mini-card"); card.append(make("small", label), make("strong", value)); return card;
  }
  function renderMetrics() {
    const metrics = state.run.metrics || {};
    const counts = countsFrom(state.run.candidates);
    const grid = $("metrics"); clear(grid);
    const k = Number.isSafeInteger(metrics.k) ? metrics.k : "K";
    const precisionCard = tierCard("Precision@" + k + "（仅相关）", metricText(metrics.precision_at_k, true));
    const ndcgCard = tierCard("候选池 nDCG@" + k, metricText(metrics.pool_ndcg_at_k, false));
    const reasons = {
      top_k_not_fully_judged: "前 K 条仍有未判断或无法判断",
      fewer_than_k_candidates: "候选不足 K 条",
      candidate_pool_not_fully_judged: "候选池仍有未判断或无法判断",
      no_relevant_or_partial_document_in_pool: "池内没有相关或部分相关结果"
    };
    if (metrics.precision_reason) precisionCard.appendChild(make("small", reasons[metrics.precision_reason] || metrics.precision_reason));
    if (metrics.pool_ndcg_reason) ndcgCard.appendChild(make("small", reasons[metrics.pool_ndcg_reason] || metrics.pool_ndcg_reason));
    grid.append(tierCard("人工判定覆盖率", metricText(state.run.progress && state.run.progress.judged_coverage != null ? state.run.progress.judged_coverage : counts.total ? counts.judged / counts.total : null, true)), precisionCard, ndcgCard);
    const tierGrid = $("tier-summary"); clear(tierGrid);
    const trace = state.run.trace || {};
    ["hot", "warm", "cold"].forEach(function (tier) {
      const finalCount = state.run.candidates.filter(function (candidate) { return candidate.tier === tier; }).length;
      let layer = trace.tiers && (Array.isArray(trace.tiers) ? trace.tiers.find(function (item) { return item.tier === tier; }) : trace.tiers[tier]);
      if (!layer && trace.layers) layer = Array.isArray(trace.layers) ? trace.layers.find(function (item) { return item.tier === tier; }) : trace.layers[tier];
      const recalled = layer && (layer.candidate_count ?? layer.retrieved_count ?? layer.recalled_count ?? layer.returned_news ?? layer.fetched);
      const card = tierCard(TIERS[tier], finalCount + " 篇最终结果");
      if (recalled != null) card.appendChild(make("small", "层内召回 " + recalled + " 篇" + (layer.candidate_limit == null ? "" : " / 上限 " + layer.candidate_limit)));
      if (layer && layer.available_news != null) card.appendChild(make("small", "该层共 " + layer.available_news + " 篇 · " + layer.available_chunks + " 切片"));
      tierGrid.appendChild(card);
    });
    const plan = trace.retrieval_plan || {};
    const intentNames = { latest: "最新进展", historical: "历史背景", timeline: "事件发展", balanced: "综合检索" };
    text("routing-summary", "路由意图：" + (intentNames[plan.intent] || plan.intent || "未提供") + " · 板块提示：" + (plan.topic || "未指定") + " · 总耗时：" + (finiteNumber(trace.total_ms) == null ? "未提供" : trace.total_ms.toFixed(0) + " ms") + (plan.category_mode === "soft_hint_no_filter" ? "。板块仅影响召回预算，结果可能跨板块。" : ""));
    const stageList = $("trace-stages"); clear(stageList);
    const stageNames = { embedding: "查询向量化", tier_search_and_news_dedup: "分层召回与同篇去重", global_cosine_fusion: "全局融合排序" };
    (Array.isArray(trace.stages) ? trace.stages : []).forEach(function (stage) {
      const row = make("p", (stageNames[stage.name] || stage.name) + " · " + (finiteNumber(stage.elapsed_ms) == null ? "耗时未知" : stage.elapsed_ms.toFixed(1) + " ms"), "fine-print");
      if (stage.output_news != null) row.appendChild(make("span", " · 输出 " + stage.output_news + " 篇"));
      stageList.appendChild(row);
    });
    text("trace-json", JSON.stringify(trace, null, 2));
    text("versions-json", JSON.stringify(state.run.versions || {}, null, 2));
    text("run-progress", progressText(state.run.progress, counts));
    $("progress-bar").style.width = (counts.total ? counts.touched / counts.total * 100 : 0) + "%";
  }
  function renderCandidateTabs() {
    const tabs = $("candidate-tabs"); clear(tabs);
    state.run.candidates.forEach(function (candidate, index) {
      const label = candidate.judgment && candidate.judgment.label;
      const button = make("button", index + 1, "candidate-tab"); button.type = "button";
      button.classList.toggle("active", index === state.index);
      button.classList.toggle("judged", Boolean(label && label !== "unsure"));
      button.classList.toggle("unable", label === "unsure");
      button.title = (index + 1) + ". " + candidate.title + " · " + (LABELS[label] || "未标注");
      button.setAttribute("aria-label", button.title);
      if (index === state.index) button.setAttribute("aria-current", "true");
      button.disabled = state.saving;
      button.addEventListener("click", function () { if (allowLeave()) { state.index = index; renderCandidate(); } });
      tabs.appendChild(button);
    });
  }
  function appendSource(parent, source) {
    const url = safeSourceUrl(typeof source === "string" ? source : source.url || source.source_url);
    const title = typeof source === "string" ? source : source.title || source.source_name || source.url || source.news_id || "其他来源";
    const item = make("li");
    if (url) { const link = make("a", title); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; item.appendChild(link); }
    else item.appendChild(make("span", title));
    parent.appendChild(item);
  }
  function renderCandidate() {
    state.dirty = false;
    text("save-status", ""); $("save-status").classList.remove("error-text"); $("reload-run").hidden = true;
    renderCandidateTabs();
    const candidate = current();
    $("article-content").hidden = !candidate;
    $("no-candidates").hidden = Boolean(candidate);
    text("candidate-position", candidate ? "候选 " + (state.index + 1) + " / " + state.run.candidates.length : "0 条候选");
    if (!candidate) { updateSaveButtons(); return; }
    text("article-title", candidate.title || "未命名新闻");
    text("article-category", candidate.category || state.run.category || "新闻");
    text("article-date", "发布：" + (candidate.publication_date || "日期未知"));
    text("article-tier", TIERS[candidate.tier] || candidate.tier || "未分层");
    const source = safeSourceUrl(candidate.url);
    $("article-source").hidden = !source;
    if (source) { $("article-source").href = source; text("article-source", new URL(source).hostname + " · 查看原始来源 ↗"); }
    else $("article-source").removeAttribute("href");
    text("article-body", candidate.body || "未取得可读正文，请结合来源判断，或选择“无法判断”。");
    text("article-score", finiteNumber(candidate.score) === null ? "模型分数未提供。" : "余弦相似度：" + candidate.score.toFixed(6) + "（不代表相关概率或准确率）");
    text("article-excerpt", candidate.matched_excerpt || "未提供匹配片段。");
    document.querySelector(".score-detail").open = false;
    const duplicates = Array.isArray(candidate.duplicate_sources) ? candidate.duplicate_sources : [];
    clear($("duplicate-list")); duplicates.forEach(function (item) { appendSource($("duplicate-list"), item); });
    $("duplicate-detail").hidden = !duplicates.length; $("duplicate-detail").open = false;
    text("duplicate-summary", "其他来源（" + duplicates.length + "）");
    const relations = Array.isArray(candidate.relations) ? candidate.relations : [];
    clear($("relation-list"));
    relations.forEach(function (relation) {
      const item = make("div", null, "relation-item");
      if (typeof relation === "string") item.appendChild(make("p", relation));
      else { item.appendChild(make("strong", relation.label || relation.relation_type || relation.type || "关系候选")); item.appendChild(make("pre", JSON.stringify(relation, null, 2))); }
      $("relation-list").appendChild(item);
    });
    $("relation-detail").hidden = !relations.length; $("relation-detail").open = false;
    const judgment = candidate.judgment;
    document.querySelectorAll('input[name="relevance"]').forEach(function (input) { input.checked = Boolean(judgment && judgment.label === input.value); });
    $("judgment-notes").value = judgment && judgment.notes || "";
    if (judgment && judgment.reviewer) $("reviewer").value = judgment.reviewer;
    text("saved-info", hasJudgment(candidate) ? "已保存：" + (LABELS[judgment.label] || judgment.label) + " · 版本 " + judgment.revision + (judgment.updated_at ? " · " + judgment.updated_at : "") : "当前新闻尚未标注");
    const hash = new URLSearchParams({ run: state.run.run_id, news: candidate.news_id });
    history.replaceState(null, "", "#" + hash.toString());
    updateSaveButtons();
  }
  function applyRun(run) {
    if (!run || !Array.isArray(run.candidates) || !run.run_id) throw new Error("召回结果格式不完整，请重试。");
    state.run = run;
    const hash = new URLSearchParams(location.hash.slice(1));
    const index = hash.get("run") === run.run_id ? run.candidates.findIndex(function (item) { return item.news_id === hash.get("news"); }) : -1;
    const firstPending = run.candidates.findIndex(function (item) { return !hasJudgment(item); });
    state.index = index >= 0 ? index : firstPending >= 0 ? firstPending : 0;
    setBusy(false); $("empty-state").hidden = true;
    text("query-text", typeof run.query === "string" ? run.query : run.query && (run.query.text || run.query.query) || "未命名查询");
    const query = state.queries.find(function (item) { return item.run_id === run.run_id; });
    text("query-category", query && query.category || run.category || "全部板块");
    renderMetrics(); renderCandidate(); syncCurrentQuery(); displayError("");
  }
  async function openRun(id) {
    if (!id) { displayError("查询尚未关联召回结果。"); return; }
    const requestId = ++state.requestId;
    setBusy(true); displayError(""); $("empty-state").hidden = true;
    try { const run = await api("/api/runs/" + encodeURIComponent(id)); if (requestId === state.requestId) applyRun(run); }
    catch (error) { if (requestId === state.requestId) { setBusy(false); displayError(errorMessage(error)); if (!state.run) $("empty-state").hidden = false; } }
  }
  function markDirty() { state.dirty = true; text("save-status", "有未保存的修改"); $("save-status").classList.remove("error-text"); }
  async function save(advance) {
    if (state.saving || !current()) return;
    const candidate = current();
    const selected = document.querySelector('input[name="relevance"]:checked');
    let payload;
    try { payload = buildJudgment(candidate, selected && selected.value, $("judgment-notes").value, $("reviewer").value); }
    catch (error) { text("save-status", errorMessage(error)); $("save-status").classList.add("error-text"); return; }
    state.saving = true; updateSaveButtons(); renderCandidateTabs(); text("save-status", "正在写入本地服务…"); $("save-status").classList.remove("error-text");
    try {
      const result = await api("/api/runs/" + encodeURIComponent(state.run.run_id) + "/judgments/" + encodeURIComponent(candidate.news_id), { method: "PUT", body: JSON.stringify(payload) });
      candidate.judgment = result.judgment; candidate.revision = result.judgment.revision;
      state.run.progress = result.progress; state.run.metrics = result.metrics;
      state.dirty = false;
      renderMetrics(); syncCurrentQuery();
      if (advance && state.index < state.run.candidates.length - 1) state.index += 1;
      renderCandidate();
      text("save-status", candidate !== current() ? (hasJudgment(current()) ? "上一条已保存，当前新闻已有标注，可查看或修改。" : "上一条已保存，当前新闻待标注。") : advance ? "已保存。此查询的标注进度已更新。" : "已保存到本地服务。");
    } catch (error) {
      text("save-status", error.status === 409 ? "这条标注已被另一页面修改。请重新加载，核对最新判断后再保存。" : "保存失败：" + errorMessage(error));
      $("save-status").classList.add("error-text"); $("reload-run").hidden = error.status !== 409;
    } finally { state.saving = false; updateSaveButtons(); renderCandidateTabs(); }
  }
  function changeCandidate(delta) { if (allowLeave()) { state.index = Math.max(0, Math.min(state.run.candidates.length - 1, state.index + delta)); renderCandidate(); } }
  function changeQuery(delta) {
    const index = state.queries.findIndex(function (query) { return state.run && query.run_id === state.run.run_id; });
    const query = state.queries[index + delta]; if (query && allowLeave()) openRun(query.run_id);
  }
  $("query-filter").addEventListener("input", renderQueries);
  $("refresh-queries").addEventListener("click", function () { refreshQueries().catch(function (error) { displayError(errorMessage(error)); }); });
  $("previous-candidate").addEventListener("click", function () { changeCandidate(-1); });
  $("next-candidate").addEventListener("click", function () { changeCandidate(1); });
  $("previous-query").addEventListener("click", function () { changeQuery(-1); });
  $("next-query").addEventListener("click", function () { changeQuery(1); });
  $("save").addEventListener("click", function () { save(false); });
  $("save-next").addEventListener("click", function () { save(true); });
  $("reload-run").addEventListener("click", function () { if (allowLeave()) openRun(state.run.run_id); });
  document.querySelectorAll('input[name="relevance"]').forEach(function (input) { input.addEventListener("change", markDirty); });
  $("judgment-notes").addEventListener("input", markDirty);
  $("reviewer").addEventListener("input", markDirty);
  window.addEventListener("beforeunload", function (event) { if (state.dirty || state.saving) { event.preventDefault(); event.returnValue = ""; } });
  document.addEventListener("keydown", function (event) {
    if (!current() || state.saving || state.loading || event.repeat) return;
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); save(true); return; }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) || event.ctrlKey || event.metaKey || event.altKey) return;
    const labels = ["relevant", "partial", "irrelevant", "unsure"];
    const label = labels[Number(event.key) - 1];
    if (label) { event.preventDefault(); document.querySelector('input[value="' + label + '"]').checked = true; markDirty(); }
  });
  $("search-form").addEventListener("submit", async function (event) {
    event.preventDefault(); if (!allowLeave() || state.loading) return;
    const query = $("custom-query").value.trim(); if (!query) return;
    const requestId = ++state.requestId; setBusy(true); text("search-status", "正在执行召回…"); displayError("");
    try {
      const payload = { query: query, limit: 10 };
      if ($("custom-category").value) payload.category = $("custom-category").value;
      const run = await api("/api/search", { method: "POST", body: JSON.stringify(payload) });
      if (requestId !== state.requestId) return;
      await refreshQueries(); applyRun(run); text("search-status", "召回完成，判断结果后点击保存。");
    } catch (error) { if (requestId === state.requestId) { setBusy(false); text("search-status", "召回失败，请重试。"); displayError(errorMessage(error)); } }
  });
  async function init() {
    try {
      await Promise.all([api("/api/status").then(renderStatus), refreshQueries()]);
      const hash = new URLSearchParams(location.hash.slice(1));
      const id = hash.get("run") || state.queries[0] && state.queries[0].run_id;
      if (id) await openRun(id); else { setBusy(false); $("empty-state").hidden = false; }
    } catch (error) { setBusy(false); displayError(errorMessage(error)); }
  }
  init();
})(typeof globalThis !== "undefined" ? globalThis : this);
