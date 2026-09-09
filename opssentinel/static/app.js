"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const names = {restart_service: "重启受管服务", rollback_release: "回退已知版本", restore_config: "恢复已知配置", rotate_logs: "轮转受管日志"};
  const statuses = {investigating: "正在调查", awaiting_approval: "等待批准", remediating: "正在处置", verifying: "验证恢复", resolved: "已恢复", escalated: "需要人工处理"};
  const sources = {rules: "规则诊断", model: "模型辅助诊断", rules_fallback: "规则回退"};
  let state = null, polling = false, settingsId = null, incidentId = null, detailSignature = "", faultPending = false, telemetryId = null;
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
  function renderSummary() {
    const {summary: s, runtime: r, services} = state;
    $("workspaceMode").textContent = r.mode === "demo" ? "包含隔离演练服务" : "真实服务监测";
    $("diagnosticMode").textContent = r.model_enabled ? "模型辅助 · 规则兜底" : "规则诊断";
    $("sidebarVersion").textContent = "OPSSENTINEL / " + r.version;
    $("footerVersion").textContent = r.version;
    $("navServiceCount").textContent = s.services;
    $("navIncidentCount").textContent = s.open_incidents;
    $("heroBanner").classList.toggle("attention", s.unhealthy > 0 || s.open_incidents > 0);
    const enabled = services.filter(service => service.enabled).length;
    $("heroTitle").textContent = !s.services ? "接入第一个服务，开始持续观测" : !enabled ? "巡检已暂停，保留最后观测" : s.awaiting_approval ? s.awaiting_approval + " 个恢复方案等待你的决定" : s.unhealthy ? s.unhealthy + " 个服务异常，正在跟踪处理" : s.open_incidents ? "服务状态正在恢复，继续验证" : s.healthy === s.services ? "服务运行正常，持续守护中" : "等待完成首次健康检查";
    $("heroDescription").textContent = enabled + " 个服务正在巡检 · " + s.open_incidents + " 个未关闭事故 · 恢复须通过连续业务检查";
    const cards = [
      ["受管服务", s.services, "▤", enabled + " 个持续巡检 · " + (services.length - enabled) + " 个已暂停", ""],
      ["最近检查健康", s.healthy, "✓", s.unhealthy + " 个异常 · 暂停项保留最后观测", "green"],
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
      const title = button(service.name, "service-name service-link", () => { telemetryId = service.id; renderTelemetry(); open("telemetryDialog"); });
      if (service.connector === "demo") title.append(node("span", "mini-tag", "演练"));
      const target = node("span", "service-target", service.target); target.title = service.target;
      info.append(title, target); cell.append(node("span", "service-icon", service.connector === "agent" ? "▤" : "◇"), info); main.append(cell);
      const health = node("td"); health.append(pill(service.health === "healthy" ? "健康" : service.health === "unhealthy" ? "异常" : "待检查", service.health));
      if (!service.enabled) health.append(node("div", "field-hint", "巡检暂停"));
      const latency = node("td"), value = service.latest?.latency_ms;
      latency.append(node("div", "latency-number", Number.isFinite(value) ? value.toFixed(1) + " ms" : "—"), sparkline(service));
      const policy = node("td"); policy.append(pill(service.connector === "http" ? "仅监测" : service.auto_actions.length ? "自动 · " + service.auto_actions.length + " 项" : "逐次批准", service.auto_actions.length ? "green" : ""));
      const controls = node("td");
      const edit = button("策略", "button secondary small", () => editService(service.id));
      edit.setAttribute("aria-label", "设置 " + service.name + " 的策略"); controls.append(edit);
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
    const signature = JSON.stringify(i) + busy.has("incident:" + i.id);
    if (signature === detailSignature) return;
    detailSignature = signature;
    const oldScroll = $("incidentDialog").scrollTop;
    const evidenceOpen = $("incidentDetail").querySelector("details")?.open;
    const badges = node("div", "detail-badges");
    badges.append(statusPill(i), pill(sources[i.diagnostic_source] || "诊断", "violet"), node("span", "", i.service_name + " · " + fmt(i.created_at, true)), node("span", "", "已尝试 " + i.attempts + " 次动作"));
    const diagnosis = node("div", "diagnosis-box"); diagnosis.append(node("strong", "", "诊断与证据"), document.createTextNode(i.diagnosis || "正在收集证据"));
    const parts = [node("h3", "detail-title", i.title), badges, diagnosis];
    if (i.status === "resolved") parts.push(node("p", "inline-note", i.resolution_kind === "mitigated" ? "系统动作成功返回后，连续业务检查通过，记录为已恢复。源代码中的根因是否修复仍需单独确认。" : "连续业务检查通过；没有可确认的本系统成功处置证据，恢复原因仍需核对。"));
    const actions = node("div", "detail-actions");
    if (i.status === "awaiting_approval" && i.action && !i.stopped_by_user) {
      const approve = button("批准本次「" + (names[i.action] || i.action) + "」", "button primary", event => run("incident:" + i.id, event.currentTarget, async () => {
        const result = await api("/api/incidents/" + encodeURIComponent(i.id) + "/approve", "POST"); toast(result.message);
      }));
      approve.disabled = busy.has("incident:" + i.id); actions.append(approve);
    }
    if (i.status !== "resolved" && !i.stopped_by_user) {
      const stop = button("停止后续自动处置", "button danger", event => run("incident:" + i.id, event.currentTarget, async () => {
        const result = await api("/api/incidents/" + encodeURIComponent(i.id) + "/dismiss", "POST", {reason: "操作员在看板停止本次自动处置"}); toast(result.message);
      }));
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
      el.disabled = !service || busy.has("fault") || (el.dataset.fault !== "recover" && (active || faultPending || !service.enabled));
    }
    if (active || service?.health === "unhealthy") faultPending = false;
    $("exerciseHint").textContent = !service ? "当前控制器未启用演练；使用 --demo 启动" : !service.enabled ? "演练服务已暂停，请先恢复巡检" : active ? "事故正在跟踪中，可在事故详情查看进度" : faultPending ? "故障已注入，等待定时巡检发现" : "准备就绪 · 选择一个故障开始演练";
  }
  function renderTelemetry() {
    const service = state.services.find(s => s.id === telemetryId); if (!service) return;
    const latest = service.latest;
    $("telemetryTitle").textContent = service.name;
    const content = $("telemetryDetail"), previousScroll = $("telemetryDialog").scrollTop;
    const badges = node("div", "detail-badges"); badges.append(pill(service.connector === "demo" ? "隔离演练" : service.connector.toUpperCase(), "violet"), pill(service.enabled ? "持续巡检" : "巡检暂停"), node("span", "", "观测于 " + fmt(service.last_check_at, true)));
    const parts = [badges, node("div", "settings-facts", service.target)];
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
  function render() { renderSummary(); renderServices(); renderActivity(); renderIncidents(); renderExercise(); if (telemetryId) renderTelemetry(); }
  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      state = await api("/api/state");
      $("connectionStatus").textContent = "控制器在线"; $("connectionStatus").className = "connection";
      $("connectionNotice").classList.add("hidden"); $("lastUpdated").textContent = "看板更新于 " + fmt(new Date().toISOString()); render();
    } catch (error) {
      $("connectionStatus").textContent = "连接未就绪"; $("connectionStatus").className = "connection error";
      $("connectionNotice").textContent = error.message + "。保留最后收到的状态，连接恢复后自动更新。";
      $("connectionNotice").classList.remove("hidden");
    } finally { polling = false; }
  }
  function editService(id) {
    const service = state.services.find(s => s.id === id); if (!service) return;
    settingsId = id; const form = $("settingsForm");
    $("settingsTitle").textContent = service.name + " · 策略"; $("settingsFacts").textContent = service.connector.toUpperCase() + " / " + service.target;
    form.elements.enabled.checked = service.enabled; form.elements.interval_seconds.value = service.interval_seconds;
    $("actionChoices").replaceChildren(...Object.entries(names).map(([action, label]) => {
      const choice = node("label", "action-choice"), input = node("input"); input.type = "checkbox"; input.name = "auto_actions"; input.value = action;
      input.checked = service.auto_actions.includes(action); input.disabled = service.connector === "http";
      choice.append(input, document.createTextNode(label)); return choice;
    })); open("settingsDialog");
  }
  for (const el of document.querySelectorAll("[data-close]")) el.addEventListener("click", () => $(el.dataset.close).close());
  const views = {overview: ["值班概览", "让服务持续在线", "发现异常、调查原因、验证恢复。每个处理阶段都清晰可见。"], services: ["受管服务", "每个服务，持续关注", "查看探测结果与处置策略，按服务设置自动化范围。"], incidents: ["事故中心", "从异常到恢复的每一步", "诊断、动作、验证与原始证据，在同一个地方追踪。"], exercise: ["故障演练", "亲眼验证一次自动恢复", "给独立示例服务注入故障，观察定时巡检与恢复闭环。"]};
  for (const nav of document.querySelectorAll("[data-view]")) nav.addEventListener("click", () => {
    const view = nav.dataset.view, [crumb, title, description] = views[view];
    for (const item of document.querySelectorAll("[data-view]")) item.classList.toggle("active", item === nav);
    $("breadcrumbView").textContent = crumb; $("pageTitle").textContent = title; $("pageDescription").textContent = description;
    $("overviewContent").classList.toggle("single", view !== "overview");
    for (const [id, own] of [["servicePanel", "services"], ["incidentPanel", "incidents"], ["activityPanel", "overview"], ["exercisePanel", "exercise"]]) $(id).classList.toggle("hidden", view !== "overview" && view !== own);
  });
  $("incidentFilter").addEventListener("change", () => { if (state) renderIncidents(); });
  function connectorFields() {
    const agent = $("connectorType").value === "agent"; $("agentFields").classList.toggle("hidden", !agent);
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
      try { await api("/api/services", "POST", body); form.reset(); $("serviceDialog").close(); toast("服务已接入，定时巡检将自动开始"); }
      catch (error) { $("serviceFormError").textContent = error.message; throw error; }
    });
  });
  $("settingsForm").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, id = settingsId;
    const body = {enabled: form.elements.enabled.checked, interval_seconds: Number(form.elements.interval_seconds.value), auto_actions: [...form.querySelectorAll('input[name="auto_actions"]:checked')].map(el => el.value)};
    run("settings", event.submitter, async () => { await api("/api/services/" + encodeURIComponent(id), "PATCH", body); $("settingsDialog").close(); toast("服务策略已保存"); });
  });
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
  $("telemetryDialog").addEventListener("close", () => { telemetryId = null; });
  setInterval(() => { $("localTime").textContent = fmt(new Date().toISOString()); }, 1000);
  setInterval(refresh, 3000); refresh();
})();
