"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const names = {restart_service: "重启受管服务", rollback_release: "回退已知版本", restore_config: "恢复已知配置", rotate_logs: "轮转受管日志"};
  const statuses = {investigating: "正在调查", awaiting_approval: "等待批准", remediating: "正在处置", verifying: "验证恢复", resolved: "已恢复", escalated: "需要人工处理"};
  const sources = {rules: "规则诊断", model: "模型辅助诊断", rules_fallback: "规则回退"};
  const metricNames = {latency_ms: "响应时间", cpu_percent: "主机 CPU 使用率", memory_percent: "主机内存使用率", disk_percent: "代理数据目录所在磁盘使用率"};
  const metricSources = {latency_ms: "连接器测得的检查耗时；主机代理包含容器、业务与日志检查，并非纯业务请求时延", cpu_percent: "主机 Agent 采集整机 CPU，非单个容器", memory_percent: "主机 Agent 采集整机内存，非单个容器", disk_percent: "主机 Agent 数据目录所在的文件系统"};
  let state = null, polling = false, settingsId = null, incidentId = null, detailSignature = "", faultPending = false, telemetryId = null;
  let maintenanceId = null, historyHours = 24, historyMetric = "latency_ms", historyData = null, historySeq = 0, historyLoadedAt = 0, historyPendingKey = "", historyPendingSeq = 0, disconnected = false;
  const busy = new Set(), samples = new Map();
  const session = {
    get(key, fallback = "") { try { return sessionStorage.getItem(key) || fallback; } catch { return fallback; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* Session storage may be disabled. */ } }
  };
  let token = session.get("opssentinel.token");
  let pausedIds;
  try { pausedIds = JSON.parse(session.get("opssentinel.paused", "[]")); } catch { pausedIds = []; }
  if (!Array.isArray(pausedIds)) pausedIds = [];
  const node = (tag, className = "", value = null) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (value !== null) el.textContent = String(value);
    return el;
  };
  const pill = (label, color = "") => node("span", "pill " + color, label);
  const fmt = (stamp, full = false) => {
    if (!stamp) return "—";
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", full ? {hour12: false} : {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  };
  function toast(message, error = false) {
    const el = node("div", "toast" + (error ? " error" : ""), message);
    $("toasts").append(el);
    setTimeout(() => el.remove(), error ? 8000 : 4500);
  }
  function open(id) { if (!$(id).open) $(id).showModal(); }
  function empty(message) {
    const el = node("div", "empty-state");
    el.append(node("span", "empty-symbol", "◇"), node("p", "", message));
    return el;
  }
  function button(label, className, fn) {
    const el = node("button", className, label);
    el.type = "button";
    el.addEventListener("click", fn);
    return el;
  }
  async function api(path, method = "GET", body) {
    const headers = {"Accept": "application/json"};
    if (token) headers.Authorization = "Bearer " + token;
    if (method !== "GET") headers["X-OpsSentinel-Request"] = "dashboard";
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), method === "GET" ? 10000 : 120000);
    try {
      const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal: controller.signal});
      if (response.status === 401) open("tokenDialog");
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败（" + response.status + "）");
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error(method === "GET" ? "读取状态超时，将自动重连" : "请求超时，操作结果待确认；请查看事故记录，不要假定操作未执行");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  async function run(key, el, task) {
    if (busy.has(key)) return;
    busy.add(key);
    if (el) el.disabled = true;
    try { await task(); } catch (error) { toast(error.message || "操作未完成", true); }
    finally { busy.delete(key); if (el?.isConnected) el.disabled = false; await refresh(); }
  }
  function statusPill(incident) {
    return pill(incident.stopped_by_user ? "已停止处置" : (statuses[incident.status] || incident.status), incident.status === "resolved" ? "green" : incident.status === "escalated" ? "red" : "amber");
  }
  function servicePill(service) {
    if (disconnected) return pill("连接中断 · 历史状态", "amber");
    const freshness = service.freshness || (!service.enabled ? "paused" : service.last_check_at ? "fresh" : "unknown");
    if (freshness === "maintenance" || service.maintenance_active) return pill("计划维护中", "violet");
    if (freshness === "paused") return pill("巡检已暂停");
    if (freshness === "stale") return pill("观测已过期", "amber");
    if (freshness === "unknown") return pill("等待首次检查");
    return pill(service.health === "healthy" ? "健康" : service.health === "unhealthy" ? "异常" : "待检查", service.health);
  }
  function maintenanceText(service) {
    if (!service.maintenance_active) return "未处于维护窗口";
    const minutes = Math.max(0, Math.ceil((new Date(service.maintenance_until).getTime() - Date.now()) / 60000));
    return "维护至 " + fmt(service.maintenance_until, true) + " · " + (minutes ? "剩余约 " + minutes + " 分钟" : "等待控制器结束维护") + (service.maintenance_reason ? " · " + service.maintenance_reason : "");
  }
  function renderSummary() {
    const {summary: s, runtime: r, services} = state;
    $("workspaceMode").textContent = r.mode === "demo" ? "包含隔离演练服务" : "真实服务监测";
    $("diagnosticMode").textContent = r.model_enabled ? "模型辅助 · 规则兜底" : "规则诊断";
    $("sidebarVersion").textContent = "OPSSENTINEL / " + r.version;
    $("footerVersion").textContent = r.version;
    $("navServiceCount").textContent = s.services;
    $("navIncidentCount").textContent = s.open_incidents;
    $("navAlertCount").textContent = s.firing_alerts || 0;
    $("heroBanner").classList.toggle("attention", disconnected || s.unhealthy > 0 || s.open_incidents > 0 || s.stale_services > 0 || s.firing_alerts > 0);
    const enabled = services.filter(service => service.enabled).length;
    $("heroTitle").textContent = !s.services ? "接入第一个服务，开始持续观测" : !enabled ? "巡检已暂停，保留最后观测" : s.awaiting_approval ? s.awaiting_approval + " 个恢复方案等待你的决定" : s.unhealthy ? s.unhealthy + " 个服务异常，正在跟踪处理" : s.open_incidents ? "服务状态正在恢复，继续验证" : s.healthy === enabled ? "当前受监测服务健康，持续守护中" : "等待完成首次健康检查";
    if (s.stale_services) $("heroTitle").textContent = s.stale_services + " 个服务观测已过期，需要关注采集状态";
    else if (s.firing_alerts && !s.unhealthy && !s.open_incidents) $("heroTitle").textContent = s.firing_alerts + " 项资源持续超限，业务健康仍独立验证";
    else if (s.maintenance_services && !s.unhealthy && !s.open_incidents) $("heroTitle").textContent = s.maintenance_services + " 个服务正在计划维护";
    if (disconnected) $("heroTitle").textContent = "控制器连接中断，当前显示最后收到的状态";
    $("heroDescription").textContent = enabled + " 个服务正在巡检 · " + (s.maintenance_services || 0) + " 个维护中 · " + (s.stale_services || 0) + " 个观测过期 · " + (s.firing_alerts || 0) + " 项持续预警";
    if (disconnected) $("heroDescription").textContent = "以下记录来自最近一次成功连接；恢复连接后自动刷新，不据此判断当前健康。";
    const cards = [
      ["受管服务", s.services, "▤", enabled + " 个持续巡检 · " + (services.length - enabled) + " 个已暂停", ""],
      ["有效观测健康", disconnected ? "—" : s.healthy, "✓", (s.stale_services || 0) + " 个过期 · 维护与暂停项不计入", disconnected ? "" : "green"],
      ["待处理事故", s.open_incidents, "⌁", s.awaiting_approval + " 个等待批准", s.open_incidents ? "amber" : ""],
      ["累计确认恢复", s.resolved_incidents, "↗", "按连续检查结果记录，含外部恢复", "green"]
    ];
    $("statsGrid").replaceChildren(...cards.map(([label, value, icon, note, color]) => {
      const card = node("div", "stat-card"), top = node("div", "stat-top");
      top.append(node("span", "", label), node("span", "stat-icon", icon));
      card.append(top, node("div", "stat-value " + color, value), node("div", "stat-note", note));
      return card;
    }));
  }
  function sparkline(service) {
    const latest = service.latest, history = samples.get(service.id) || [];
    if (latest && Number.isFinite(latest.latency_ms) && service.last_check_at && history.at(-1)?.stamp !== service.last_check_at) {
      history.push({stamp: service.last_check_at, value: latest.latency_ms});
      if (history.length > 24) history.shift();
      samples.set(service.id, history);
    }
    if (history.length < 2) return document.createTextNode("");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "sparkline"); svg.setAttribute("viewBox", "0 0 64 18"); svg.setAttribute("aria-label", "本次页面会话中实际响应时间趋势");
    const line = document.createElementNS(svg.namespaceURI, "polyline");
    const max = Math.max(1, ...history.map(x => x.value));
    line.setAttribute("points", history.map((x, i) => (i * 62 / (history.length - 1) + 1) + "," + (16 - x.value / max * 13)).join(" "));
    line.setAttribute("fill", "none"); line.setAttribute("stroke", "#58ac97"); line.setAttribute("stroke-width", "1.4");
    svg.append(line); return svg;
  }
  function renderServices() {
    $("serviceCount").textContent = state.services.length + " 个服务";
    $("serviceRows").replaceChildren(...state.services.map(service => {
      const row = node("tr"), main = node("td"), cell = node("div", "service-cell"), info = node("div");
      const title = button(service.name, "service-name service-link", () => showTelemetry(service.id));
      title.dataset.focusKey = "telemetry:" + service.id;
      if (service.connector === "demo") title.append(node("span", "mini-tag", "演练"));
      const target = node("span", "service-target", service.target); target.title = service.target;
      info.append(title, target); cell.append(node("span", "service-icon", service.connector === "agent" ? "▤" : "◇"), info); main.append(cell);
      const health = node("td"); health.append(servicePill(service));
      if (service.maintenance_active) { const note = node("div", "field-hint", "至 " + fmt(service.maintenance_until)); note.title = maintenanceText(service); health.append(note); }
      const latency = node("td"), value = service.latest?.latency_ms;
      latency.append(node("div", "latency-number", Number.isFinite(value) ? value.toFixed(1) + " ms" : "—"), sparkline(service));
      const policy = node("td"); policy.append(pill(service.connector === "http" ? "仅监测" : service.auto_actions.length ? "自动 · " + service.auto_actions.length + " 项" : "逐次批准", service.auto_actions.length ? "green" : ""));
      const controls = node("td");
      const edit = button("策略", "button secondary small", () => editService(service.id));
      edit.dataset.focusKey = "settings:" + service.id;
      const maintain = button("维护", "text-button", () => showMaintenance(service.id)); maintain.dataset.focusKey = "maintain:" + service.id;
      maintain.setAttribute("aria-label", "设置 " + service.name + " 的维护窗口");
      edit.setAttribute("aria-label", "设置 " + service.name + " 的策略"); controls.append(edit, maintain);
      row.append(main, health, latency, policy, node("td", "", fmt(service.last_check_at)), controls); return row;
    }));
    if (!state.services.length) {
      const row = node("tr"), cell = node("td"); cell.colSpan = 6;
      cell.append(empty("接入 HTTP 健康检查或 Linux 主机 Agent")); row.append(cell); $("serviceRows").append(row);
    }
    $("pauseAll").textContent = pausedIds.length ? "恢复本次暂停的巡检" : "暂停全部巡检";
    $("pauseAll").disabled = busy.has("pause") || (!pausedIds.length && !state.services.some(s => s.enabled));
  }
  function renderActivity() {
    $("activityList").replaceChildren(...state.recent_events.slice(0, 12).map(event => {
      const item = node("div", "activity-item"), body = node("div");
      body.append(node("div", "activity-message", event.message), node("div", "activity-time", fmt(event.created_at)));
      item.append(node("span", "activity-dot " + (/^[a-z_]+$/.test(event.kind) ? event.kind : "")), body); return item;
    }));
    if (!state.recent_events.length) $("activityList").append(empty("还没有运行事件"));
  }
  function renderIncidents() {
    const filter = $("incidentFilter").value;
    const incidents = state.incidents.filter(i => filter === "all" || (filter === "active" ? i.status !== "resolved" : i.status === filter));
    $("incidentList").replaceChildren(...incidents.map(incident => {
      const card = button("", "incident-card", () => showIncident(incident.id));
      card.dataset.focusKey = "incident-card:" + incident.id;
      const body = node("div", "incident-card-body"), meta = node("div", "incident-meta");
      meta.append(statusPill(incident), node("span", "", incident.service_name), node("span", "", fmt(incident.created_at)));
      body.append(node("div", "incident-card-title", incident.title), meta);
      card.append(node("span", "incident-signal" + (incident.status === "resolved" ? " done" : ""), incident.status === "resolved" ? "✓" : "!"), body, node("span", "arrow", "›"));
      return card;
    }));
    if (!incidents.length) $("incidentList").append(empty(filter === "all" ? "暂无事故。发现连续异常时会自动建立记录。" : "这个分类中暂无事故"));
    renderDetail();
  }
  function showIncident(id) { incidentId = id; detailSignature = ""; renderDetail(); open("incidentDialog"); }
  function renderDetail() {
    if (!incidentId) return;
    const i = state.incidents.find(x => x.id === incidentId);
    if (!i) return;
    const service = state.services.find(s => s.id === i.service_id);
    const signature = JSON.stringify(i) + busy.has("incident:" + i.id) + service?.maintenance_active;
    if (signature === detailSignature) return;
    detailSignature = signature;
    const oldScroll = $("incidentDialog").scrollTop;
    const evidenceOpen = $("incidentDetail").querySelector("details")?.open;
    const badges = node("div", "detail-badges");
    badges.append(statusPill(i), pill(sources[i.diagnostic_source] || "诊断", "violet"), node("span", "", i.service_name + " · " + fmt(i.created_at, true)), node("span", "", "已尝试 " + i.attempts + " 次动作"));
    const diagnosis = node("div", "diagnosis-box"); diagnosis.append(node("strong", "", "诊断与证据"), document.createTextNode(i.diagnosis || "正在收集证据"));
    const parts = [node("h3", "detail-title", i.title), badges, diagnosis];
    if (service?.maintenance_active) parts.push(node("p", "inline-note maintenance-note", maintenanceText(service) + "。当前仅观测，所有恢复动作与审批均已暂停。"));
    if (i.status === "resolved") parts.push(node("p", "inline-note", i.resolution_kind === "mitigated" ? "系统动作成功返回后，连续业务检查通过，记录为已恢复。源代码中的根因是否修复仍需单独确认。" : "连续业务检查通过；没有可确认的本系统成功处置证据，恢复原因仍需核对。"));
    const actions = node("div", "detail-actions");
    if (i.status === "awaiting_approval" && i.action && !i.stopped_by_user) {
      const approve = button("批准本次「" + (names[i.action] || i.action) + "」", "button primary", event => run("incident:" + i.id, event.currentTarget, async () => {
        const result = await api("/api/incidents/" + encodeURIComponent(i.id) + "/approve", "POST", {plan_id: i.proposal?.plan_id}); toast(result.message);
      }));
      approve.dataset.focusKey = "approve:" + i.id;
      approve.disabled = busy.has("incident:" + i.id) || Boolean(service?.maintenance_active); actions.append(approve);
    }
    if (i.status !== "resolved" && !i.stopped_by_user) {
      const stop = button("停止后续自动处置", "button danger", event => run("incident:" + i.id, event.currentTarget, async () => {
        const result = await api("/api/incidents/" + encodeURIComponent(i.id) + "/dismiss", "POST", {reason: "操作员在看板停止本次自动处置"}); toast(result.message);
      }));
      stop.dataset.focusKey = "stop:" + i.id;
      stop.disabled = busy.has("incident:" + i.id) || i.status === "remediating"; actions.append(stop);
      if (i.status === "remediating") parts.push(node("p", "inline-note", "当前动作已经开始，不能从看板中途撤销。结果返回后可停止后续自动处置。"));
    }
    actions.append(button("导出事故证据", "button secondary", () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(i, null, 2)], {type: "application/json"}));
      const link = node("a"); link.href = url; link.download = "opssentinel-incident-" + i.id + ".json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
    parts.push(actions);
    const timeline = node("div", "timeline");
    for (const event of i.events || []) {
      const item = node("div", "timeline-item"); item.append(node("div", "timeline-time", fmt(event.created_at, true)), node("div", "timeline-message", event.message)); timeline.append(item);
    }
    const evidence = node("details", "evidence"); evidence.open = Boolean(evidenceOpen);
    evidence.append(node("summary", "", "查看原始观测与执行前后证据"), node("pre", "", JSON.stringify(i.evidence || {}, null, 2)));
    parts.push(timeline, evidence); $("incidentDetail").replaceChildren(...parts); $("incidentDialog").scrollTop = oldScroll;
  }
  function renderExercise() {
    const service = state.services.find(s => s.id === "demo-service");
    const active = state.incidents.some(i => i.service_id === "demo-service" && i.status !== "resolved");
    for (const el of document.querySelectorAll("[data-fault]")) {
      el.disabled = !service || busy.has("fault") || (el.dataset.fault !== "recover" && (active || faultPending || !service.enabled || service.maintenance_active));
    }
    if (active || service?.health === "unhealthy") faultPending = false;
    $("exerciseHint").textContent = !service ? "当前控制器未启用演练；使用 --demo 启动" : service.maintenance_active ? "演练服务处于维护中，请先结束维护" : !service.enabled ? "演练服务已暂停，请先恢复巡检" : active ? "事故正在跟踪中，可在事故详情查看进度" : faultPending ? "故障已注入，等待定时巡检发现" : "准备就绪 · 选择一个故障开始演练";
  }
  function renderTelemetry() {
    const service = state.services.find(s => s.id === telemetryId); if (!service) return;
    const latest = service.latest;
    $("telemetryTitle").textContent = service.name;
    const content = $("telemetryDetail"), previousScroll = $("telemetryDialog").scrollTop;
    const badges = node("div", "detail-badges"); badges.append(pill(service.connector === "demo" ? "隔离演练" : service.connector === "agent" ? "主机代理" : "网站与接口", "violet"), servicePill(service), node("span", "", "最近采样 " + fmt(service.last_check_at, true)));
    const parts = [badges, node("div", "settings-facts", service.target)];
    if (service.maintenance_active) parts.push(node("p", "inline-note maintenance-note", maintenanceText(service)));
    if (service.freshness === "stale" || disconnected) parts.push(node("p", "inline-note", "以下为最后一次观测，不能据此判断服务当前健康。请检查控制器与主机连接。"));
    if (!latest) parts.push(empty("等待首次观测"));
    else {
      parts.push(node("p", "diagnosis-box", latest.summary));
      const labels = {cpu_percent: "主机 CPU 使用率", memory_percent: "主机内存使用率", disk_percent: "代理数据目录所在磁盘使用率", log_bytes: "受管日志大小"};
      const metrics = node("div", "telemetry-metrics");
      for (const key of ["cpu_percent", "memory_percent", "disk_percent"]) {
        const card = node("div", "stat-card"), value = latest.metrics?.[key];
        card.append(node("div", "stat-note", labels[key]), node("div", "stat-value", Number.isFinite(value) ? value.toFixed(1) + "%" : "—")); metrics.append(card);
      }
      parts.push(metrics);
      if (service.connector !== "agent") parts.push(node("p", "field-hint", "此连接器不采集真实主机资源指标。接入 Linux 主机 Agent 后显示 CPU、内存与磁盘观测。"));
      const checks = node("div", "telemetry-checks");
      for (const check of latest.checks || []) {
        const row = node("div", "telemetry-check"); row.append(pill(check.ok ? "通过" : "失败", check.ok ? "green" : "red"), node("strong", "", check.name), node("span", "", check.detail)); checks.append(row);
      }
      parts.push(node("h3", "", "业务与服务检查"), checks);
      if (latest.logs?.length) parts.push(node("h3", "", "最近日志（已脱敏）"), node("pre", "telemetry-logs", latest.logs.join("\n")));
      const raw = node("details", "evidence"); raw.open = Boolean(content.querySelector("details")?.open);
      raw.append(node("summary", "", "查看完整观测"), node("pre", "", JSON.stringify(latest, null, 2))); parts.push(raw);
    }
    content.replaceChildren(...parts); $("telemetryDialog").scrollTop = previousScroll;
  }
  function render() {
    const focusKey = document.activeElement?.dataset.focusKey;
    renderSummary(); renderServices(); renderActivity(); renderIncidents(); renderAlerts(); renderExercise(); renderMaintenanceState(); if (telemetryId) renderTelemetry();
    if (focusKey) {
      const target = [...document.querySelectorAll("[data-focus-key]")].find(el => el.dataset.focusKey === focusKey);
      if (target && !target.disabled && document.activeElement !== target) target.focus({preventScroll: true});
    }
  }
  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      state = await api("/api/state");
      disconnected = false;
      $("connectionStatus").textContent = "控制器在线"; $("connectionStatus").className = "connection";
      $("connectionNotice").classList.add("hidden"); $("lastUpdated").textContent = "看板更新于 " + fmt(new Date().toISOString()); render();
    } catch (error) {
      disconnected = true;
      $("connectionStatus").textContent = "连接未就绪"; $("connectionStatus").className = "connection error";
      $("connectionNotice").textContent = error.message + "。保留最后收到的状态，连接恢复后自动更新。";
      $("connectionNotice").classList.remove("hidden");
      if (state) { renderSummary(); renderServices(); if (telemetryId) renderTelemetry(); }
    } finally { polling = false; }
  }
  function editService(id) {
    const service = state.services.find(s => s.id === id); if (!service) return;
    settingsId = id; const form = $("settingsForm");
    $("settingsTitle").textContent = service.name + " · 策略"; $("settingsFacts").textContent = service.connector.toUpperCase() + " / " + service.target;
    form.elements.name.value = service.name;
    form.elements.enabled.checked = service.enabled; form.elements.interval_seconds.value = service.interval_seconds;
    $("editHttpProbe").classList.toggle("hidden", service.connector !== "http");
    $("editHttpProbe").disabled = service.connector !== "http";
    setHttpFields(form, service.http_probe || {});
    buildRules(service);
    $("actionChoices").replaceChildren(...Object.entries(names).map(([action, label]) => {
      const choice = node("label", "action-choice"), input = node("input"); input.type = "checkbox"; input.name = "auto_actions"; input.value = action;
      input.checked = service.auto_actions.includes(action); input.disabled = service.connector === "http";
      choice.append(input, document.createTextNode(label)); return choice;
    })); open("settingsDialog");
  }
  function renderAlerts() {
    const filter = $("alertFilter").value;
    const alerts = (state.resource_alerts || []).filter(a => filter === "all" || a.status === filter);
    $("alertList").replaceChildren(...alerts.map(alert => {
      const card = node("article", "alert-card"), top = node("div", "alert-top"), title = node("div");
      const serviceLink = button(alert.service_name, "service-name service-link", () => showTelemetry(alert.service_id)); serviceLink.dataset.focusKey = "alert-service:" + alert.id;
      title.append(serviceLink, node("div", "alert-metric", metricNames[alert.metric] || "资源指标"));
      top.append(title, pill(alert.status === "firing" ? "持续超限" : alert.resolution_reason === "rule_changed" ? "策略变更结束" : "已恢复", alert.status === "firing" ? "amber" : alert.resolution_reason === "rule_changed" ? "" : "green"));
      const stats = node("p", "alert-values", "最近值 " + metricValue(alert.value, alert.metric) + " · 峰值 " + metricValue(alert.peak_value, alert.metric));
      card.append(top, stats, node("p", "field-hint", "连续 " + alert.for_checks + " 次高于 " + metricValue(alert.above, alert.metric) + " 触发；连续低于 " + metricValue(alert.recover_below, alert.metric) + " 恢复。"));
      const service = state.services.find(s => s.id === alert.service_id);
      if (service && (service.freshness !== "fresh" || disconnected)) card.append(servicePill(service));
      if (alert.status === "firing" && !Number.isFinite(alert.value)) card.append(node("p", "inline-note", "最新采样缺少该指标，保留预警，尚无恢复证据。"));
      const bottom = node("div", "alert-bottom");
      bottom.append(node("span", "", "开始于 " + fmt(alert.created_at, true)));
      if (alert.acknowledged_at) bottom.append(pill("已确认知悉"));
      else if (alert.status === "firing") {
        const ack = button("确认知悉", "button secondary small", event => run("alert:" + alert.id, event.currentTarget, async () => {
          await api("/api/resource-alerts/" + encodeURIComponent(alert.id) + "/acknowledge", "POST", {note: "操作员已在看板确认知悉"}); toast("已记录知悉；持续超限状态仍保留");
        }));
        ack.dataset.focusKey = "ack:" + alert.id;
        ack.disabled = busy.has("alert:" + alert.id); bottom.append(ack);
      }
      card.append(bottom); return card;
    }));
    if (!alerts.length) $("alertList").append(empty(filter === "firing" ? "当前没有持续超限预警。可在服务策略中设置阈值。" : "暂无符合条件的预警记录"));
  }
  function metricValue(value, metric) { return Number.isFinite(value) ? value.toLocaleString("zh-CN", {maximumFractionDigits: 1}) + (metric === "latency_ms" ? " ms" : "%") : "—"; }
  function inputLabel(text, name, options = {}) {
    const label = node("label", "field-label", text), input = node("input"); input.name = name;
    for (const [key, value] of Object.entries(options)) { input[key] = value; if (key === "value") input.defaultValue = value; }
    label.append(input); return label;
  }
  function httpFields(id) {
    const group = node("fieldset", "probe-fields"); group.id = id;
    group.append(node("legend", "", "HTTP 业务检查"));
    const row = node("div", "form-row");
    row.append(inputLabel("请求超时（秒）", "http_timeout", {type: "number", min: "1", max: "30", step: "0.1", value: "5", required: true}), inputLabel("预期状态码（可选）", "http_status", {type: "number", min: "100", max: "599", placeholder: "留空接受任意 2xx"}));
    group.append(row, inputLabel("响应需包含的文本（可选）", "http_body", {maxLength: 500, placeholder: "例如：ready"}), node("p", "field-hint", "检查有限长度的响应内容；响应正文不会写入日志和证据。留空即不匹配正文。")); return group;
  }
  function setHttpFields(form, probe) {
    form.elements.http_timeout.value = probe.timeout_seconds ?? 5;
    form.elements.http_status.value = probe.expected_status ?? "";
    form.elements.http_body.value = probe.body_contains || "";
  }
  function readHttpFields(form) {
    return {timeout_seconds: Number(form.elements.http_timeout.value), expected_status: form.elements.http_status.value === "" ? null : Number(form.elements.http_status.value), body_contains: form.elements.http_body.value};
  }
  function buildRules(service) {
    $("resourceRuleChoices").replaceChildren(...Object.entries(metricNames).map(([metric, title]) => {
      const rule = (service.resource_rules || []).find(r => r.metric === metric), supported = metric === "latency_ms" || service.connector === "agent";
      const fieldset = node("fieldset", "rule-fieldset"), legend = node("legend"), toggle = node("input");
      toggle.type = "checkbox"; toggle.name = "rule_enabled"; toggle.value = metric; toggle.checked = Boolean(rule); toggle.disabled = !supported;
      const toggleLabel = node("label", "rule-toggle"); toggleLabel.append(toggle, document.createTextNode(title)); legend.append(toggleLabel); fieldset.append(legend);
      const row = node("div", "rule-inputs");
      row.append(inputLabel("触发线 >", metric + "_above", {type: "number", min: "0", max: metric === "latency_ms" ? "60000" : "100", step: "0.1", value: rule?.above ?? (metric === "latency_ms" ? 1000 : 90), required: true}), inputLabel("恢复线 <", metric + "_recover", {type: "number", min: "0", max: metric === "latency_ms" ? "60000" : "100", step: "0.1", value: rule?.recover_below ?? (metric === "latency_ms" ? 800 : 85), required: true}), inputLabel("连续次数", metric + "_checks", {type: "number", min: "1", max: "20", value: rule?.for_checks ?? 3, required: true}));
      const update = () => { for (const input of row.querySelectorAll("input")) input.disabled = !supported || !toggle.checked; };
      toggle.addEventListener("change", update); update();
      fieldset.append(row, node("p", "field-hint", supported ? "数据来源：" + metricSources[metric] + (metric === "latency_ms" ? "；单位毫秒。" : "；单位百分比。") : "此接入方式不采集该指标，接入主机代理后可设置。")); return fieldset;
    }));
  }
  function readRules(form) {
    return [...form.querySelectorAll('input[name="rule_enabled"]:checked')].map(toggle => {
      const metric = toggle.value, above = Number(form.elements[metric + "_above"].value), recover = Number(form.elements[metric + "_recover"].value);
      if (!(recover < above)) throw new Error(metricNames[metric] + "：恢复线必须低于触发线");
      return {metric, above, recover_below: recover, for_checks: Number(form.elements[metric + "_checks"].value)};
    });
  }
  function showMaintenance(id) {
    const service = state.services.find(s => s.id === id); if (!service) return;
    maintenanceId = id; $("maintenanceForm").reset(); $("maintenanceForm").elements.reason.value = service.maintenance_reason || "";
    $("maintenanceTitle").textContent = service.name + " · 计划维护"; renderMaintenanceState(); open("maintenanceDialog");
  }
  function renderMaintenanceState() {
    const service = state?.services.find(s => s.id === maintenanceId); if (!service) return;
    $("maintenanceState").textContent = maintenanceText(service);
    $("endMaintenance").disabled = !service.maintenance_active || busy.has("maintenance");
    $("startMaintenance").textContent = service.maintenance_active ? "更新维护窗口" : "开始维护";
  }
  function showTelemetry(id) {
    if (!state.services.some(s => s.id === id)) return;
    telemetryId = id; historyData = null; historyLoadedAt = 0; historySeq++;
    clearHistorySummary();
    renderTelemetry(); open("telemetryDialog"); fetchHistory(true);
  }
  async function fetchHistory(force = false) {
    if (!telemetryId || !$("telemetryDialog").open) return;
    const id = telemetryId, hours = historyHours, key = id + ":" + hours;
    if ((!force && Date.now() - historyLoadedAt < 15000) || historyPendingKey === key) return;
    const sequence = ++historySeq; historyPendingKey = key; historyPendingSeq = sequence;
    $("historyNotice").textContent = historyData ? "正在更新历史…" : "正在读取持久化历史…";
    if (!historyData) $("historyChart").replaceChildren(empty("读取历史采样"));
    try {
      const data = await api("/api/services/" + encodeURIComponent(id) + "/history?hours=" + hours);
      if (sequence !== historySeq || telemetryId !== id || historyHours !== hours || !$("telemetryDialog").open) return;
      historyData = data; historyLoadedAt = Date.now(); renderHistory();
    } catch (error) {
      if (sequence === historySeq && telemetryId === id && historyHours === hours) {
        $("historyNotice").textContent = error.message + (historyData ? "。图表仍显示上次读取结果。" : "。稍后自动重试。");
        if (!historyData) $("historyChart").replaceChildren(empty("历史暂时不可用"));
      }
    } finally { if (historyPendingSeq === sequence) historyPendingKey = ""; }
  }
  function renderHistory() {
    if (!historyData) return;
    const {summary, points, bucket_seconds: bucket} = historyData;
    $("historyNotice").textContent = "历史已持久化 · 曲线显示每 " + (bucket < 60 ? bucket + " 秒" : Math.round(bucket / 60) + " 分钟") + "采样均值；断线表示没有采样，不填补为 0。紫点包含维护期采样。";
    $("historySummary").replaceChildren(...[["采样通过率", Number.isFinite(summary.success_rate) ? summary.success_rate.toFixed(1) + "%" : "—"], ["实际采样数", summary.sample_count], ["响应时间 P95", metricValue(summary.p95_latency_ms, "latency_ms")]].map(([label, value]) => {
      const item = node("div", "history-summary-item"); item.append(node("span", "", label), node("strong", "", value)); return item;
    }));
    $("historyRange").textContent = "数据起止：" + fmt(summary.first_at, true) + " → " + fmt(summary.last_at, true) + "。采样通过率仅统计已采集的探针结果，不代表可用性或 SLA。";
    const chart = $("historyChart"), ordered = [...points].sort((a, b) => new Date(a.at) - new Date(b.at)), usable = ordered.filter(p => Number.isFinite(p[historyMetric]));
    const service = state.services.find(s => s.id === telemetryId);
    $("historySource").textContent = "数据来源：" + metricSources[historyMetric] + (service?.connector !== "agent" && historyMetric !== "latency_ms" ? "；此服务未采集该指标。" : "。");
    if (!usable.length) { chart.replaceChildren(empty("所选时间段没有此指标的实际采样")); $("historyValues").replaceChildren(); return; }
    const svgNS = "http://www.w3.org/2000/svg", svgNode = (tag, attrs = {}) => { const el = document.createElementNS(svgNS, tag); for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value)); return el; };
    const svg = svgNode("svg", {viewBox: "0 0 700 235", role: "img", "aria-label": metricNames[historyMetric] + "历史均值曲线，具体数值可展开下方表格"});
    const title = svgNode("title"); title.textContent = metricNames[historyMetric] + " · 每个点为真实采样桶均值"; svg.append(title);
    const end = Date.now(), start = end - historyHours * 3600000, max = Math.max(historyMetric === "latency_ms" ? 1 : 100, ...usable.map(p => p[historyMetric])) * 1.05;
    const x = stamp => 54 + Math.max(0, Math.min(1, (new Date(stamp).getTime() - start) / (end - start))) * 630, y = value => 190 - value / max * 160;
    for (let tick = 0; tick <= 4; tick++) {
      const value = max * tick / 4, cy = y(value), label = svgNode("text", {x: 46, y: cy + 3, "text-anchor": "end", class: "chart-label"}); label.textContent = value.toFixed(historyMetric === "latency_ms" && max < 10 ? 1 : 0);
      svg.append(svgNode("line", {x1: 54, x2: 684, y1: cy, y2: cy, class: "chart-grid"}), label);
    }
    for (let tick = 0; tick <= 4; tick++) {
      const time = start + (end - start) * tick / 4, label = svgNode("text", {x: 54 + tick * 157.5, y: 215, "text-anchor": tick === 0 ? "start" : tick === 4 ? "end" : "middle", class: "chart-label"});
      label.textContent = new Date(time).toLocaleString("zh-CN", historyHours === 168 ? {month: "numeric", day: "numeric", hour: "2-digit"} : {hour: "2-digit", minute: "2-digit", hour12: false}); svg.append(label);
    }
    let segment = [], lastAt = null;
    const drawSegment = () => { if (segment.length > 1) svg.append(svgNode("polyline", {points: segment.join(" "), fill: "none", stroke: "#178c74", "stroke-width": 2, "stroke-linejoin": "round"})); segment = []; };
    for (const point of ordered) {
      const time = new Date(point.at).getTime(), value = point[historyMetric];
      if (!Number.isFinite(value)) { drawSegment(); lastAt = null; continue; }
      if (lastAt !== null && time - lastAt > bucket * 1500) drawSegment();
      segment.push(x(point.at) + "," + y(value)); lastAt = time;
    }
    drawSegment();
    for (const point of usable) {
      const circle = svgNode("circle", {cx: x(point.at), cy: y(point[historyMetric]), r: usable.length > 60 ? 2 : 3, fill: point.maintenance_samples ? "#9373cb" : "#178c74"});
      const hint = svgNode("title"); hint.textContent = fmt(point.at, true) + " · 均值 " + metricValue(point[historyMetric], historyMetric) + " · " + point.samples + " 次采样" + (point.maintenance_samples ? " · 含维护期采样" : ""); circle.append(hint); svg.append(circle);
    }
    chart.replaceChildren(svg);
    const table = node("table", "history-table"), head = node("thead"), header = node("tr");
    for (const label of ["采样桶时间", "均值", "采样数", "业务通过", "维护期采样"]) header.append(node("th", "", label)); head.append(header); table.append(head);
    const body = node("tbody");
    for (const point of [...ordered].reverse()) { const row = node("tr"); for (const value of [fmt(point.at, true), metricValue(point[historyMetric], historyMetric), point.samples, point.healthy_samples, point.maintenance_samples]) row.append(node("td", "", value)); body.append(row); }
    table.append(body); $("historyValues").replaceChildren(table);
  }
  function initializeFormsAndHistory() {
    const addProbe = httpFields("addHttpProbe"); $("agentFields").before(addProbe);
    const settings = $("settingsForm"); $("settingsFacts").after(inputLabel("服务名称", "name", {required: true, maxLength: 80}));
    const editProbe = httpFields("editHttpProbe"), rules = node("section", "resource-rules");
    rules.append(node("h3", "", "持续资源预警"), node("p", "field-hint", "预警只记录持续超限，不会触发重启。恢复线应低于触发线，防止状态反复切换。"));
    const ruleChoices = node("div"); ruleChoices.id = "resourceRuleChoices"; rules.append(ruleChoices);
    settings.querySelector("h3").before(editProbe, rules);
    const history = node("section", "history-section"), heading = node("div", "history-heading");
    heading.append(node("h3", "", "历史趋势"), node("span", "small-tag", "保存 7 天"));
    const controls = node("div", "history-controls");
    for (const [id, title, options] of [["historyHours", "时间范围", [[1, "最近 1 小时"], [24, "最近 24 小时"], [168, "最近 7 天"]]], ["historyMetric", "观测指标", Object.entries(metricNames)]]) {
      const label = node("label", "field-label", title), select = node("select"); select.id = id;
      for (const [value, text] of options) { const option = node("option", "", text); option.value = value; select.append(option); }
      label.append(select); controls.append(label);
    }
    const summary = node("div", "history-summary"); summary.id = "historySummary";
    const notice = node("p", "field-hint"); notice.id = "historyNotice"; notice.setAttribute("role", "status");
    const chart = node("div", "history-chart"); chart.id = "historyChart";
    const source = node("p", "field-hint"); source.id = "historySource";
    const range = node("p", "field-hint"); range.id = "historyRange";
    const values = node("details", "evidence history-values"); values.append(node("summary", "", "查看采样桶明细（新到旧）"));
    const table = node("div", "table-scroll"); table.id = "historyValues"; values.append(table);
    history.append(heading, controls, summary, notice, chart, source, range, values);
    $("telemetryDetail").before(history, node("h3", "", "最近一次观测"));
    $("historyHours").value = historyHours;
  }
  initializeFormsAndHistory();
  for (const el of document.querySelectorAll("[data-close]")) el.addEventListener("click", () => $(el.dataset.close).close());
  const views = {overview: ["值班概览", "让服务持续在线", "持续采样、资源预警、计划维护。每个处理阶段都清晰可见。"], services: ["受管服务", "每个服务，持续关注", "查看持久化历史与处置策略，按服务配置业务检查和阈值。"], incidents: ["事故中心", "从异常到恢复的每一步", "诊断、动作、验证与原始证据，在同一个地方追踪。"], alerts: ["资源预警", "在故障前，看见持续压力", "以连续采样验证超限；确认知悉保留预警，实际指标恢复后才关闭。"], exercise: ["故障演练", "亲眼验证一次自动恢复", "给独立示例服务注入故障，观察定时巡检与恢复闭环。"]};
  for (const nav of document.querySelectorAll("[data-view]")) nav.addEventListener("click", () => {
    const view = nav.dataset.view, [crumb, title, description] = views[view];
    for (const item of document.querySelectorAll("[data-view]")) { item.classList.toggle("active", item === nav); item.setAttribute("aria-current", item === nav ? "page" : "false"); }
    $("breadcrumbView").textContent = crumb; $("pageTitle").textContent = title; $("pageDescription").textContent = description;
    $("overviewContent").classList.toggle("single", view !== "overview");
    for (const [id, own] of [["servicePanel", "services"], ["incidentPanel", "incidents"], ["alertPanel", "alerts"], ["activityPanel", "overview"], ["exercisePanel", "exercise"]]) $(id).classList.toggle("hidden", view !== "overview" && view !== own);
  });
  $("incidentFilter").addEventListener("change", () => { if (state) renderIncidents(); });
  $("alertFilter").addEventListener("change", () => { if (state) renderAlerts(); });
  function connectorFields() {
    const agent = $("connectorType").value === "agent"; $("agentFields").classList.toggle("hidden", !agent);
    $("addHttpProbe").classList.toggle("hidden", agent); $("addHttpProbe").disabled = agent;
    $("targetLabel").textContent = agent ? "主机 Agent 地址" : "健康检查 URL";
    $("serviceForm").elements.agent_service.required = agent; $("serviceForm").elements.agent_token.required = agent;
  }
  $("connectorType").addEventListener("change", connectorFields);
  $("addServiceOpen").addEventListener("click", () => { $("serviceForm").reset(); $("serviceFormError").textContent = ""; connectorFields(); open("serviceDialog"); });
  $("serviceForm").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, data = new FormData(form);
    run("add", event.submitter, async () => {
      const body = {name: data.get("name"), connector: data.get("connector"), target: data.get("target"), interval_seconds: Number(data.get("interval_seconds")), failure_threshold: Number(data.get("failure_threshold")), auto_actions: []};
      if (body.connector === "agent") { body.agent_service = data.get("agent_service"); body.agent_token = data.get("agent_token"); }
      else body.http_probe = readHttpFields(form);
      try { await api("/api/services", "POST", body); form.reset(); $("serviceDialog").close(); toast("服务已接入，定时巡检将自动开始"); }
      catch (error) { $("serviceFormError").textContent = error.message; throw error; }
    });
  });
  $("settingsForm").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, id = settingsId;
    run("settings", event.submitter, async () => {
      const body = {name: form.elements.name.value, enabled: form.elements.enabled.checked, interval_seconds: Number(form.elements.interval_seconds.value), auto_actions: [...form.querySelectorAll('input[name="auto_actions"]:checked')].map(el => el.value), resource_rules: readRules(form)};
      if (state.services.find(s => s.id === id)?.connector === "http") body.http_probe = readHttpFields(form);
      await api("/api/services/" + encodeURIComponent(id), "PATCH", body); $("settingsDialog").close(); toast("服务策略已保存");
    });
  });
  $("maintenanceForm").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, id = maintenanceId;
    run("maintenance", event.submitter, async () => { await api("/api/services/" + encodeURIComponent(id) + "/maintenance", "POST", {minutes: Number(form.elements.minutes.value), reason: form.elements.reason.value}); $("maintenanceDialog").close(); toast("维护窗口已设置，继续采样并暂停恢复动作"); });
  });
  $("endMaintenance").addEventListener("click", event => {
    const id = maintenanceId;
    run("maintenance", event.currentTarget, async () => { await api("/api/services/" + encodeURIComponent(id) + "/maintenance", "DELETE"); $("maintenanceDialog").close(); toast("维护已结束，重新积累连续检查证据"); });
  });
  $("historyHours").addEventListener("change", event => { historyHours = Number(event.target.value); historyData = null; historyLoadedAt = 0; historySeq++; clearHistorySummary(); fetchHistory(true); });
  $("historyMetric").addEventListener("change", event => { historyMetric = event.target.value; renderHistory(); });
  function clearHistorySummary() { $("historySummary").replaceChildren(); $("historyRange").textContent = ""; $("historySource").textContent = ""; $("historyValues").replaceChildren(); }
  $("scanAll").addEventListener("click", event => run("scan", event.currentTarget, async () => {
    if (!state?.services.length) return toast("请先接入服务");
    const results = await Promise.allSettled(state.services.map(s => api("/api/services/" + encodeURIComponent(s.id) + "/scan", "POST")));
    const failed = results.filter(r => r.status === "rejected");
    toast(failed.length ? (results.length - failed.length) + " 个服务巡检完成；" + failed[0].reason.message : "本次巡检已完成", Boolean(failed.length));
  }));
  $("pauseAll").addEventListener("click", event => run("pause", event.currentTarget, async () => {
    if (!state) return;
    const resume = pausedIds.length > 0;
    const ids = resume ? [...pausedIds] : state.services.filter(s => s.enabled).map(s => s.id);
    const failed = [];
    for (const id of ids) {
      try {
        await api("/api/services/" + encodeURIComponent(id), "PATCH", {enabled: resume});
        pausedIds = resume ? pausedIds.filter(x => x !== id) : [...new Set([...pausedIds, id])];
        session.set("opssentinel.paused", JSON.stringify(pausedIds));
      } catch (error) { failed.push(error.message); }
    }
    toast(failed.length ? "部分策略未更新：" + failed[0] : resume ? "已恢复本次暂停的服务" : "已暂停巡检，状态与事故记录保留", Boolean(failed.length));
  }));
  for (const el of document.querySelectorAll("[data-fault]")) el.addEventListener("click", event => run("fault", event.currentTarget, async () => {
    const fault = el.dataset.fault; const result = await api("/api/demo/fault", "POST", {fault});
    faultPending = fault !== "recover"; toast(result.summary || "演练状态已更新");
  }));
  $("tokenOpen").addEventListener("click", () => { $("controllerToken").value = token; open("tokenDialog"); });
  $("tokenForm").addEventListener("submit", event => { event.preventDefault(); token = $("controllerToken").value.trim(); session.set("opssentinel.token", token); $("controllerToken").value = ""; $("tokenDialog").close(); refresh(); });
  $("incidentDialog").addEventListener("close", () => { incidentId = null; detailSignature = ""; });
  $("telemetryDialog").addEventListener("close", () => { telemetryId = null; historySeq++; });
  $("maintenanceDialog").addEventListener("close", () => { maintenanceId = null; });
  setInterval(() => { $("localTime").textContent = fmt(new Date().toISOString()); }, 1000);
  setInterval(refresh, 3000); setInterval(() => fetchHistory(), 5000); refresh();
})();
