const state = {
  summary: null,
  business: null,
  platform: null,
  traces: null,
  schema: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "-")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatMoney(value) {
  return `¥${formatNumber(value, 2)}`;
}

function formatTime(value) {
  if (!value) return "-";
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  const date = new Date(hasTimezone ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function statusBadge(status) {
  const safe = escapeHtml(status || "unknown");
  return `<span class="status-badge ${safe}">${safe}</span>`;
}

function emptyRow(columns, label = "暂无记录") {
  return `<tr class="empty-row"><td colspan="${columns}">${escapeHtml(label)}</td></tr>`;
}

async function getJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function showError(error) {
  const banner = $("#error-banner");
  banner.textContent = `数据读取失败：${error.message || error}`;
  banner.hidden = false;
}

function clearError() {
  $("#error-banner").hidden = true;
}

function renderSummary() {
  const { business_counts: business, platform_counts: platform } = state.summary;
  const profile = state.summary.dataset_profile || {};
  $("#generated-at").textContent = `刷新于 ${formatTime(state.summary.generated_at)}`;
  $("#overview-metrics").innerHTML = [
    ["采购订单", business.orders, `${formatNumber(business.lines)} 条物料行 · ${formatNumber(profile.suppliers)} 家供应商`],
    ["问答记录", platform.interactions, `${platform.conversations} 个会话`],
    ["Workflow Run", platform.workflow_runs, `${platform.tool_calls} 次工具调用`],
    ["来源证据", platform.evidence, `${platform.feedback} 条用户反馈`],
  ].map(([label, value, note]) => `<div class="metric"><span>${label}</span><strong>${formatNumber(value)}</strong><small>${note}</small></div>`).join("");

  const recent = state.summary.recent_requests;
  $("#recent-request-rows").innerHTML = recent.length ? recent.map((item) => `
    <tr>
      <td>${formatTime(item.created_at)}</td>
      <td><code>${escapeHtml(item.request_short)}</code></td>
      <td class="question-cell">${escapeHtml(item.question)}</td>
      <td>${escapeHtml(item.intent)}</td>
      <td>${statusBadge(item.status)}</td>
      <td>${escapeHtml(item.error_code)}</td>
    </tr>`).join("") : emptyRow(6);
}

function renderBusiness() {
  const preview = state.business.preview || {};
  $("#business-preview-note").textContent = `为保证页面流畅，当前预览最近 ${formatNumber(preview.orders?.returned || state.business.orders.length)} 张订单、${formatNumber(preview.lines?.returned || state.business.lines.length)} 条物料行和 ${formatNumber(preview.documents?.returned || state.business.documents.length)} 张关联单据；全量记录数请查看数据总览或表结构。`;
  renderOrders();
  $("#line-rows").innerHTML = state.business.lines.length ? state.business.lines.map((item) => `
    <tr><td>${escapeHtml(item.order_number)}</td><td>${item.line_no}</td><td>${escapeHtml(item.material_name)}<br><span class="muted">${escapeHtml(item.material_code)}</span></td><td class="number">${formatNumber(item.ordered_qty, 2)}</td><td class="number">${formatNumber(item.received_qty, 2)}</td><td class="number">${formatNumber(item.inbound_qty, 2)}</td><td>${escapeHtml(item.unit)}</td><td class="number">${formatMoney(item.line_amount)}</td><td>${escapeHtml(item.warehouse_name)}</td></tr>`).join("") : emptyRow(9);
  $("#document-rows").innerHTML = state.business.documents.length ? state.business.documents.map((item) => `
    <tr><td>${escapeHtml(item.order_number)}</td><td>${escapeHtml(item.document_type_label)}</td><td>${escapeHtml(item.document_number)}</td><td>${escapeHtml(item.status_code)}</td><td>${escapeHtml(item.business_date)}</td><td>${escapeHtml(item.source_line_no)}</td></tr>`).join("") : emptyRow(6);
  $("#metric-rows").innerHTML = state.business.period_metrics.length ? state.business.period_metrics.map((item) => `
    <tr><td>${escapeHtml(item.period_label)}<br><span class="muted">${escapeHtml(item.period_key)}</span></td><td class="number">${formatMoney(item.purchase_amount)}</td><td class="number">${formatNumber(item.order_count)}</td><td class="number">${formatMoney(item.average_order_amount)}</td><td class="number">${formatNumber(item.on_time_rate, 2)}%</td></tr>`).join("") : emptyRow(5);
  $("#definition-rows").innerHTML = state.business.metric_definitions.length ? state.business.metric_definitions.map((item) => `
    <tr><td>${escapeHtml(item.label)}<br><span class="muted">${escapeHtml(item.metric_key)}</span></td><td>${escapeHtml(item.version)}</td><td>${escapeHtml(item.unit)}</td><td>${escapeHtml(item.definition)}</td><td><code>${escapeHtml(item.formula)}</code></td><td>${escapeHtml(item.allowed_dimensions)}</td></tr>`).join("") : emptyRow(6);
  renderDimensionBars();
}

function renderOrders() {
  const term = $("#order-search").value.trim().toLowerCase();
  const filtered = state.business.orders.filter((item) => {
    const searchable = [item.order_number, item.supplier_name, item.buyer_name, item.purchase_org_name].join(" ").toLowerCase();
    return !term || searchable.includes(term);
  });
  $("#order-rows").innerHTML = filtered.length ? filtered.map((item) => `
    <tr><td><strong>${escapeHtml(item.order_number)}</strong><br><span class="muted">${escapeHtml(item.order_date)}</span></td><td>${escapeHtml(item.supplier_name)}</td><td>${escapeHtml(item.purchase_org_name)}</td><td>${escapeHtml(item.buyer_name)}</td><td>${escapeHtml(item.business_status_code)}</td><td>${escapeHtml(item.logistics_status_code)}</td><td class="number">${formatMoney(item.total_amount)}</td><td>${escapeHtml(item.access_scope)}</td><td>${escapeHtml(item.owner_user_id)}</td></tr>`).join("") : emptyRow(9, "没有匹配的订单");
}

function renderDimensionBars() {
  const groups = Object.groupBy ? Object.groupBy(state.business.dimensions, (item) => item.dimension_type) : state.business.dimensions.reduce((acc, item) => {
    (acc[item.dimension_type] ||= []).push(item);
    return acc;
  }, {});
  const labels = { category: "品类构成", supplier: "供应商构成" };
  $("#dimension-bars").innerHTML = Object.entries(groups).map(([type, items]) => {
    const max = Math.max(...items.map((item) => Number(item.purchase_amount) || 0), 1);
    return `<div class="dimension-group ${escapeHtml(type)}"><h3>${labels[type] || escapeHtml(type)}</h3>${items.map((item) => `
      <div class="bar-row"><span class="bar-label">${escapeHtml(item.dimension_name)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, Number(item.purchase_amount) / max * 100).toFixed(1)}%"></div></div><span class="bar-value">${formatMoney(item.purchase_amount)}</span></div>`).join("")}</div>`;
  }).join("");
}

function renderPlatform() {
  $("#interaction-rows").innerHTML = state.platform.interactions.length ? state.platform.interactions.map((item) => `
    <tr><td>${formatTime(item.created_at)}</td><td><code>${escapeHtml(item.request_short)}</code></td><td class="question-cell">${escapeHtml(item.question)}</td><td>${escapeHtml(item.intent)}</td><td>${statusBadge(item.status)}</td><td class="number">${formatNumber(item.source_count)}</td><td>${escapeHtml(item.error_code)}</td></tr>`).join("") : emptyRow(7);
  $("#evidence-rows").innerHTML = state.platform.evidence.length ? state.platform.evidence.map((item) => `
    <tr><td><code>${escapeHtml(item.request_short)}</code></td><td>${escapeHtml(item.source_id)}</td><td class="question-cell">${escapeHtml(item.title)}</td><td>${escapeHtml(item.source_system)}</td><td>${escapeHtml(item.authority_level)}</td><td class="number">${formatNumber(item.score, 4)}</td><td class="number">${formatNumber(item.content_chars)}</td></tr>`).join("") : emptyRow(7);
  $("#feedback-rows").innerHTML = state.platform.feedback.length ? state.platform.feedback.map((item) => `
    <tr><td>${formatTime(item.created_at)}</td><td><code>${escapeHtml(item.request_short)}</code></td><td>${escapeHtml(item.rating)}</td><td>${escapeHtml(item.reason_codes)}</td><td class="question-cell">${escapeHtml(item.comment)}</td><td>${escapeHtml(item.user_id)}</td></tr>`).join("") : emptyRow(6, "当前还没有用户反馈记录");
  $("#workflow-run-rows").innerHTML = state.platform.workflow_runs.length ? state.platform.workflow_runs.map((item) => `
    <tr><td>${formatTime(item.started_at)}</td><td><code>${escapeHtml(item.request_short)}</code></td><td><strong>${escapeHtml(item.workflow_id)}</strong></td><td>${escapeHtml(item.workflow_version)}</td><td>${escapeHtml(item.user_id)}<br><span class="muted">${escapeHtml(item.tenant_id)} · ${escapeHtml(item.org_code)}</span></td><td>${statusBadge(item.status)}</td><td class="number">${formatNumber(item.node_count)}</td><td class="number">${formatNumber(item.tool_call_count)}</td><td class="number">${formatNumber(item.denied_count)}</td></tr>`).join("") : emptyRow(9, "当前还没有 Workflow 运行记录");
  $("#tool-call-rows").innerHTML = state.platform.tool_calls.length ? state.platform.tool_calls.map((item) => `
    <tr><td>${formatTime(item.started_at)}</td><td><code>${escapeHtml(item.request_short)}</code></td><td>${escapeHtml(item.node_id)}</td><td><strong>${escapeHtml(item.tool_id)}</strong><br><span class="muted">v${escapeHtml(item.tool_version)}</span></td><td>${escapeHtml(item.connector_id)}</td><td>${statusBadge(item.status)}</td><td class="number">${formatNumber(item.duration_ms, 1)} ms</td><td class="attribute-cell">${escapeHtml(item.arguments)}</td></tr>`).join("") : emptyRow(8, "当前还没有工具调用记录");
  $("#policy-decision-rows").innerHTML = state.platform.policy_decisions.length ? state.platform.policy_decisions.map((item) => `
    <tr><td>${formatTime(item.created_at)}</td><td><code>${escapeHtml(item.request_short)}</code></td><td>${escapeHtml(item.user_id)}</td><td><strong>${escapeHtml(item.action)}</strong></td><td>${escapeHtml(item.resource)}</td><td>${statusBadge(item.allowed ? "allowed" : "denied")}</td><td>${escapeHtml(item.policy_id)}<br><span class="muted">${escapeHtml(item.policy_version)}</span></td><td class="question-cell">${escapeHtml(item.reason)}</td></tr>`).join("") : emptyRow(8, "当前还没有权限决策记录");
}

function renderTraceOptions() {
  const select = $("#trace-select");
  const previous = select.value;
  select.innerHTML = "";
  state.traces.requests.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.request_id;
    option.textContent = `${item.request_short} · ${item.status} · ${item.question.slice(0, 28)}`;
    select.appendChild(option);
  });
  if (previous && state.traces.requests.some((item) => item.request_id === previous)) select.value = previous;
}

async function loadTraceDetail(requestId) {
  if (!requestId) {
    $("#trace-summary").innerHTML = "";
    $("#trace-rows").innerHTML = emptyRow(6);
    return;
  }
  const detail = await getJson(`/api/traces/${encodeURIComponent(requestId)}`);
  const interaction = detail.interaction;
  const root = detail.spans.find((span) => span.name === "chat.request");
  const errorCount = detail.spans.filter((span) => span.status === "error").length;
  $("#trace-summary").innerHTML = `
    <div class="trace-summary-item trace-question"><span>用户问题</span><strong>${escapeHtml(interaction.question)}</strong></div>
    <div class="trace-summary-item"><span>最终状态</span><strong>${escapeHtml(interaction.status)}</strong></div>
    <div class="trace-summary-item"><span>总耗时</span><strong>${formatNumber(root?.duration_ms || 0, 1)} ms</strong></div>
    <div class="trace-summary-item"><span>异常 Span</span><strong>${errorCount}</strong></div>`;
  $("#trace-rows").innerHTML = detail.spans.length ? detail.spans.map((span) => `
    <tr><td><strong>${escapeHtml(span.name)}</strong></td><td>${escapeHtml(span.kind)}</td><td>${statusBadge(span.status)}</td><td class="number">${formatNumber(span.duration_ms, 1)} ms</td><td>${escapeHtml(span.error_code)}</td><td class="attribute-cell">${escapeHtml(JSON.stringify(span.attributes))}</td></tr>`).join("") : emptyRow(6);
}

function populateSchemaTables() {
  const databaseKey = $("#schema-database").value;
  const tables = state.schema.databases[databaseKey].tables;
  const select = $("#schema-table");
  const previous = select.value;
  select.innerHTML = "";
  tables.forEach((table) => {
    const option = document.createElement("option");
    option.value = table.name;
    option.textContent = `${table.name} · ${table.row_count} rows`;
    select.appendChild(option);
  });
  if (previous && tables.some((table) => table.name === previous)) select.value = previous;
  renderSchemaTable();
}

function renderSchemaTable() {
  const databaseKey = $("#schema-database").value;
  const database = state.schema.databases[databaseKey];
  const table = database.tables.find((item) => item.name === $("#schema-table").value) || database.tables[0];
  if (!table) return;
  $("#schema-meta").innerHTML = `<span>数据库：<strong>${escapeHtml(database.info.name)}</strong></span><span>表：<strong>${escapeHtml(table.name)}</strong></span><span>记录数：<strong>${formatNumber(table.row_count)}</strong></span><span>字段数：<strong>${formatNumber(table.columns.length)}</strong></span>`;
  $("#schema-rows").innerHTML = table.columns.map((column) => `<tr><td><code>${escapeHtml(column.name)}</code></td><td>${escapeHtml(column.type)}</td><td>${column.primary_key ? "是" : "否"}</td><td>${column.nullable ? "是" : "否"}</td></tr>`).join("");
}

function wireTabs() {
  $$(".nav-button").forEach((button) => button.addEventListener("click", () => {
    $$(".nav-button").forEach((item) => item.classList.toggle("is-active", item === button));
    $$(".view").forEach((view) => view.classList.toggle("is-active", view.id === `view-${button.dataset.view}`));
  }));
  $$("[data-business-tab]").forEach((button) => button.addEventListener("click", () => {
    $$("[data-business-tab]").forEach((item) => item.classList.toggle("is-active", item === button));
    $$("#view-business .subview").forEach((view) => view.classList.toggle("is-active", view.id === `business-${button.dataset.businessTab}`));
  }));
  $$("[data-platform-tab]").forEach((button) => button.addEventListener("click", () => {
    $$("[data-platform-tab]").forEach((item) => item.classList.toggle("is-active", item === button));
    $$("#view-platform .subview").forEach((view) => view.classList.toggle("is-active", view.id === `platform-${button.dataset.platformTab}`));
  }));
}

async function loadAll() {
  clearError();
  $("#refresh-button").disabled = true;
  try {
    const [summary, business, platform, traces, schema] = await Promise.all([
      getJson("/api/summary"), getJson("/api/business"), getJson("/api/platform"), getJson("/api/traces"), getJson("/api/schema"),
    ]);
    Object.assign(state, { summary, business, platform, traces, schema });
    renderSummary();
    renderBusiness();
    renderPlatform();
    renderTraceOptions();
    populateSchemaTables();
    await loadTraceDetail($("#trace-select").value);
  } catch (error) {
    showError(error);
  } finally {
    $("#refresh-button").disabled = false;
  }
}

wireTabs();
$("#refresh-button").addEventListener("click", loadAll);
$("#order-search").addEventListener("input", renderOrders);
$("#trace-select").addEventListener("change", (event) => loadTraceDetail(event.target.value).catch(showError));
$("#schema-database").addEventListener("change", populateSchemaTables);
$("#schema-table").addEventListener("change", renderSchemaTable);
loadAll();
