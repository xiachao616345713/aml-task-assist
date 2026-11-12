(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { ownerId: "", token: "", hostOrigin: "", conversationId: "", lastTrace: "", selectedTrace: "",
    prefill: null, sourceUrl: "", pendingSource: "", connected: false, traceOpen: false, materialId: "", workflowId: "", busy: false };
  const statusName = { inspecting: "正在核查", waiting_material: "等待补材料", awaiting_human: "等待人工确认",
    awaiting_business: "请回原系统办理", completed: "已提交", uncertain: "结果待核对", blocked: "无法办理" };
  // getRandomValues 在普通 HTTP 页面也可用，避免随机 ID 初始化阻断所有按钮。
  const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
  let sourceLoad = 0;
  let allowedOrigins = new Set([location.origin]);

  function notice(message, error = false) {
    const box = $("notice");
    box.textContent = message;
    box.style.background = error ? "#fff0ed" : "#ecf8f5";
    box.style.color = error ? "#943b30" : "#176e61";
    box.classList.remove("hidden");
    clearTimeout(notice.timer);
    notice.timer = setTimeout(() => box.classList.add("hidden"), 7000);
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("X-Access-Token", state.token);
    else if (window.parent === window) headers.set("X-Use-Saved-Token", "true");
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...options, headers, credentials: "omit" });
    const trace = response.headers.get("X-Trace-Id");
    if (trace) state.lastTrace = trace;
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("服务响应格式异常"); }
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data));
    return data.data === undefined ? data : data.data;
  }

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function empty(container, text) { container.replaceChildren(node("div", "muted", text)); }

  async function loadConversations() {
    const result = await api("/api/conversations");
    const list = $("conversations"); list.replaceChildren();
    for (const item of result.records) {
      const button = node("button", "conversation-item" + (item.id === state.conversationId ? " active" : ""), item.title);
      button.onclick = () => openConversation(item.id);
      list.append(button);
    }
    if (!result.records.length) empty(list, "还没有对话");
  }

  function showMessages(messages) {
    const list = $("messages"); list.replaceChildren();
    if (!messages.length) {
      const emptyBox = node("div", "empty-state");
      emptyBox.append(node("div", "empty-icon", "✦"), node("h2", "", "开始处理今天的任务"),
        node("p", "", "可以问“我有多少待办？”或在右侧选择一个任务。"));
      list.append(emptyBox); return;
    }
    for (const msg of messages) {
      const bubble = node("div", "message " + msg.role, msg.content);
      if (msg.trace_id) {
        const link = node("button", "message-meta text-button", "查看 Trace " + msg.trace_id.slice(0, 10));
        link.onclick = () => openTraceView(msg.trace_id).catch((err) => notice(err.message, true));
        bubble.append(link);
      }
      list.append(bubble);
    }
    list.scrollTop = list.scrollHeight;
  }

  async function openConversation(id) {
    showChatView();
    state.conversationId = id;
    const detail = await api("/api/conversations/" + encodeURIComponent(id));
    showMessages(detail.messages);
    await loadConversations();
  }

  async function newConversation() {
    const item = await api("/api/conversations", { method: "POST" });
    await openConversation(item.id);
  }

  async function sendMessage(event) {
    event.preventDefault();
    if (state.busy) return;
    const question = $("question").value.trim();
    if (!question) return;
    state.busy = true; $("sendButton").disabled = true;
    try {
      if (!state.conversationId) await newConversation();
      $("question").value = "";
      const list = $("messages");
      if (list.querySelector(".empty-state")) list.replaceChildren();
      list.append(node("div", "message user", question));
      const pending = node("div", "message assistant pending", "正在查询和分析…"); list.append(pending);
      const result = await api("/api/conversations/" + state.conversationId + "/messages", {
        method: "POST", body: JSON.stringify({ question })
      });
      pending.textContent = result.answer;
      pending.classList.remove("pending");
      list.scrollTop = list.scrollHeight;
    } catch (err) { notice(err.message, true); await openConversation(state.conversationId).catch(() => {}); }
    finally { state.busy = false; $("sendButton").disabled = false; }
  }

  async function loadSummary() {
    const data = await api("/api/tasks/summary");
    $("taskTotal").textContent = data.total;
    $("taskTypes").textContent = data.by_type.length + " 类流程待处理";
  }

  async function loadRiskTasks() {
    const list = $("riskTasks"); list.replaceChildren();
    try {
      const data = await api("/api/tasks/risk?page=1&page_size=20");
      for (const item of data.records) {
        const card = node("div", "task-card");
        card.append(node("div", "task-card-title", (item.task_name || "风险审核") + " · " + item.task_id));
        const grade = item.risk_level;
        card.append(node("div", "task-card-meta", grade.grade + " · " + grade.reason));
        const actions = node("div", "task-card-actions");
        const button = node("button", "mini-button", grade.can_one_click ? "开始自动审核" : "查看补材料流程");
        button.onclick = () => startReview("risk", item.task_id, item.act_id);
        actions.append(button); card.append(actions); list.append(card);
      }
      if (!data.records.length) empty(list, "暂无风险待办");
    } catch (err) { empty(list, "风险列表不可用：" + err.message); }
  }

  async function loadBnfTasks() {
    const list = $("bnfTasks"); list.replaceChildren();
    try {
      const data = await api("/api/tasks/bnf?page=1&page_size=20");
      for (const item of data.records) {
        const card = node("div", "task-card");
        card.append(node("div", "task-card-title", item.task_name + " · " + item.task_id));
        card.append(node("div", "task-card-meta", item.reason));
        const button = node("button", "mini-button", "发起补材料任务");
        button.onclick = () => startReview("bnfValidate", item.task_id, item.act_id);
        card.append(button); list.append(card);
      }
      if (!data.records.length) empty(list, "暂无受益所有人核验待办");
    } catch (err) { empty(list, "核验列表不可用：" + err.message); }
  }

  async function startReview(processType, taskId, actId) {
    try {
      const data = await api("/api/workflows/reviews", { method: "POST",
        body: JSON.stringify({ process_type: processType, task_id: taskId, act_id: actId,
          conversation_id: state.conversationId || null }) });
      if (data.workflow.status === "waiting_material") notice("已创建补充材料任务，上传后可继续流程");
      else if (data.workflow.status === "completed") notice("任务已提交审批");
      else notice("审核流程状态：" + (statusName[data.workflow.status] || data.workflow.status));
      await refreshTasks();
    } catch (err) { notice(err.message, true); }
  }

  async function loadMaterialTasks() {
    const data = await api("/api/material-tasks");
    const list = $("materialTasks"); list.replaceChildren();
    for (const item of data.records) {
      const card = node("div", "task-card");
      card.append(node("div", "task-card-title", item.required.join("、")));
      card.append(node("div", "task-card-meta", "状态：" + ({ open: "等待上传", submitted: "正在恢复", resumed: "已提交" }[item.status] || item.status) + " · 已上传 " + item.attachments.length + " 份"));
      const button = node("button", "mini-button", item.status === "open" ? "整理资料 / 上传并继续" : "查看资料与预填");
      button.onclick = () => openMaterial(item.id).catch((err) => notice(err.message, true));
      card.append(button);
      list.append(card);
    }
    if (!data.records.length) empty(list, "暂无待补材料");
  }

  async function loadWorkflows() {
    const data = await api("/api/workflows");
    const list = $("workflows"); list.replaceChildren();
    for (const item of data.records) {
      const card = node("div", "task-card");
      card.append(node("div", "task-card-title", item.process_type + " · " + item.task_id));
      card.append(node("div", "task-card-meta", "状态：" + (statusName[item.status] || item.status)));
      if (item.status === "awaiting_human") {
        const button = node("button", "mini-button", "人工审核并提交");
        button.onclick = () => {
          state.workflowId = item.id;
          $("decisionReason").value = state.prefill?.workflow_id === item.id ? state.prefill.fields.reason || "" : "";
          $("decisionDialog").showModal();
        };
        card.append(button);
      }
      list.append(card);
    }
    if (!data.records.length) empty(list, "暂无审核流程");
  }

  async function openMaterial(id) {
    state.materialId = id;
    const task = await api("/api/material-tasks/" + id);
    $("dialogTitle").textContent = "补充材料";
    $("dialogDescription").textContent = task.required.join("；");
    const files = $("dialogFiles"); files.replaceChildren();
    renderFiles(files, task.attachments);
    $("prefillResult").replaceChildren();
    for (const id of ["uploadButton", "submitMaterial", "materialFile"]) $(id).disabled = task.status !== "open";
    $("materialDialog").showModal();
  }

  async function uploadMaterial() {
    const file = $("materialFile").files[0];
    if (!file || !state.materialId) return notice("请选择材料文件", true);
    const form = new FormData(); form.append("file", file);
    try {
      await api("/api/material-tasks/" + state.materialId + "/files", { method: "POST", body: form });
      $("materialFile").value = "";
      await openMaterialRefresh(); notice("材料已上传");
    } catch (err) { notice(err.message, true); }
  }

  async function openMaterialRefresh() {
    const task = await api("/api/material-tasks/" + state.materialId);
    const files = $("dialogFiles"); files.replaceChildren();
    renderFiles(files, task.attachments);
    state.prefill = null;
    $("prefillResult").replaceChildren();
  }

  function renderFiles(container, files) {
    for (const item of files) {
      const button = node("button", "file-row text-button", item.filename + " · 查看来源");
      button.type = "button";
      button.onclick = () => openSource(item.id).catch((err) => notice(err.message, true));
      container.append(button);
    }
  }

  async function generatePrefill() {
    const materialId = state.materialId;
    $("generatePrefill").disabled = true;
    try {
      const result = await api("/api/material-tasks/" + materialId + "/prefill", { method: "POST" });
      if (state.materialId !== materialId || !$("materialDialog").open) return;
      const box = $("prefillResult"); box.replaceChildren();
      box.append(node("h3", "", "资料预填草稿"), node("p", "", "核实资料事实后，可修改草稿再填入审批表单。"));
      const editor = node("textarea"); editor.rows = 6; editor.maxLength = 1000;
      editor.value = result.summary; editor.setAttribute("aria-label", "资料整理草稿"); box.append(editor);
      for (const citation of result.provenance) {
        const link = node("button", "file-row text-button", citation.filename + "：“" + citation.quote + "”");
        link.type = "button";
        link.onclick = () => openSource(citation.source_id, citation).catch((err) => notice(err.message, true));
        box.append(link);
      }
      if (result.unreadable_sources.length) box.append(node("p", "", "部分资料尚无可提取正文，请点击文件核对原件；扫描件与图片需要 OCR，历史附件需要重新上传。"));
      const apply = node("button", "secondary", "将草稿预填到业务表单"); apply.type = "button";
      apply.onclick = () => {
        const payload = { ...result, summary: editor.value, user_edited: editor.value !== result.summary };
        payload.fields = { ...result.fields };
        if (result.process_type === "risk") payload.fields.reason = editor.value;
        state.prefill = payload;
        if (state.hostOrigin) window.parent.postMessage({ type: "aml-agent-prefill", prefill: payload }, state.hostOrigin);
        notice(result.process_type === "risk" ? "草稿已就绪，打开人工审核时将填入意见；请核对后再提交" : "附件与整理摘要已交给业务页面，具体字段由原表单填写");
      };
      box.append(apply);
    } catch (err) { notice(err.message, true); }
    finally { $("generatePrefill").disabled = false; }
  }

  async function openSource(id, citation) {
    const load = ++sourceLoad;
    const source = await api("/api/sources/" + encodeURIComponent(id));
    if (load !== sourceLoad) return;
    if (state.sourceUrl) URL.revokeObjectURL(state.sourceUrl);
    state.sourceUrl = "";
    $("sourceTitle").textContent = source.filename;
    $("sourceMeta").textContent = "SHA-256: " + source.sha256;
    const box = $("sourceContent"); box.replaceChildren();
    if (source.text_content) {
      const pre = node("pre", "source-text");
      // API 偏移按 Unicode 字符计数，避免 emoji 的 UTF-16 代理对造成定位偏移。
      const chars = Array.from(source.text_content);
      if (citation && citation.sha256 === source.sha256 && chars.slice(citation.start, citation.end).join("") === citation.quote) {
        pre.append(document.createTextNode(chars.slice(0, citation.start).join("")),
          node("mark", "", citation.quote), document.createTextNode(chars.slice(citation.end).join("")));
      } else pre.textContent = source.text_content;
      box.append(pre);
    }
    $("downloadSource").disabled = true;
    if (!$("sourceDialog").open) $("sourceDialog").showModal();
    if (source.original_available) {
      const response = await fetch(source.original_path, { headers: state.token ? { "X-Access-Token": state.token } : (window.parent === window ? { "X-Use-Saved-Token": "true" } : {}), credentials: "omit" });
      if (!response.ok) throw new Error("原资料读取失败");
      const bytes = await response.arrayBuffer();
      if (load !== sourceLoad) return;
      state.sourceUrl = URL.createObjectURL(new Blob([bytes], { type: source.content_type }));
      if (["application/pdf", "image/png", "image/jpeg"].includes(source.content_type)) {
        const preview = node(source.content_type === "application/pdf" ? "iframe" : "img", "source-preview");
        preview.src = state.sourceUrl;
        if (preview.tagName === "IFRAME") { preview.title = source.filename; preview.referrerPolicy = "no-referrer"; }
        else preview.alt = source.filename;
        box.append(preview);
      }
      $("downloadSource").disabled = false;
      $("downloadSource").onclick = () => {
        const link = node("a"); link.href = state.sourceUrl; link.download = source.filename; link.click();
      };
    } else box.append(node("p", "", "此历史附件尚无本地原件，请在原业务页面打开或重新上传。"));
    box.querySelector("mark")?.scrollIntoView({ block: "center" });
  }

  async function submitMaterial() {
    try {
      const result = await api("/api/material-tasks/" + state.materialId + "/submit", { method: "POST" });
      $("materialDialog").close();
      notice(result.workflow.status === "awaiting_human" ? "材料已接收，请完成高风险人工审核" :
        result.workflow.status === "awaiting_business" ? "材料已接收，请回原系统完成受益所有人核验" : "材料已接收，流程已继续");
      if (result.workflow.status === "awaiting_business" && state.hostOrigin) {
        const handoff = await api("/api/workflows/" + result.workflow.id + "/handoff");
        window.parent.postMessage({ type: "aml-agent-handoff", handoff }, state.hostOrigin);
      }
      await refreshTasks();
    } catch (err) { notice(err.message, true); }
  }

  async function submitDecision() {
    const reason = $("decisionReason").value.trim();
    if (reason.length < 20) return notice("审核意见至少 20 字", true);
    try {
      await api("/api/workflows/" + state.workflowId + "/decision", { method: "POST", body: JSON.stringify({ reason }) });
      $("decisionDialog").close(); $("decisionReason").value = "";
      notice("高风险任务已提交审批"); await refreshTasks();
    } catch (err) { notice(err.message, true); }
  }

  async function refreshTasks() {
    await Promise.allSettled([loadSummary(), loadRiskTasks(), loadBnfTasks(), loadMaterialTasks(), loadWorkflows()]);
  }

  function showChatView() {
    state.traceOpen = false;
    $("chatView").classList.remove("hidden");
    $("traceView").classList.add("hidden");
    $("traceLink").textContent = "调用链";
  }

  async function openTraceView(traceId = "") {
    state.traceOpen = true;
    $("chatView").classList.add("hidden");
    $("traceView").classList.remove("hidden");
    $("traceLink").textContent = "返回对话";
    $("traceQuery").value = traceId;
    await loadTraces($("traceQuery").value.trim());
  }

  async function loadTraces(query = "") {
    const exact = /^[a-f0-9]{32}$/.test(query);
    const data = await api(exact ? "/api/traces?trace_id=" + query : "/api/traces?limit=50");
    const records = exact ? data.records : data.records.filter((item) => !query || item.trace_id.includes(query));
    const list = $("traceList"); list.replaceChildren();
    for (const item of records) {
      const button = node("button", "trace-list-item" + (item.trace_id === state.selectedTrace ? " active" : ""));
      button.append(node("strong", "", item.name || "调用链"),
        node("span", "", item.trace_id.slice(0, 12) + " · " + (item.status || "ok")),
        node("div", "muted", new Date(item.created_at * 1000).toLocaleString()));
      button.onclick = () => selectTrace(item.trace_id);
      list.append(button);
    }
    if (!records.length) empty(list, "没有找到当前用户的调用链");
    if (exact && records.length) await selectTrace(query);
    else if (!state.selectedTrace && records.length) await selectTrace(records[0].trace_id);
  }

  async function selectTrace(id) {
    state.selectedTrace = id;
    const data = await api("/api/traces/" + encodeURIComponent(id));
    const detail = $("traceDetail"); detail.replaceChildren();
    detail.append(node("h2", "", data.name || "调用链"),
      node("div", "trace-meta", "Trace ID: " + data.trace_id),
      node("div", "trace-meta", "状态: " + (data.status || "ok") + " · " + new Date(data.created_at * 1000).toLocaleString()));
    if (data.conversation_id) detail.append(node("div", "trace-meta", "会话: " + data.conversation_id));
    if (data.workflow_id) detail.append(node("div", "trace-meta", "工作流: " + data.workflow_id));
    const usage = data.total_tokens || {};
    detail.append(node("div", "trace-meta", "Token: 输入 " + (usage.input || 0) + " / 输出 " + (usage.output || 0) + " / 合计 " + (usage.total || 0)));
    if (data.dropped_events) detail.append(node("div", "trace-meta trace-error", "事件达到保存上限，较早的 " + data.dropped_events + " 条已移除"));
    if (data.decision_summary) detail.append(node("div", "trace-summary", "决策摘要：" + data.decision_summary));
    if (data.reasoning_summary) detail.append(node("div", "trace-summary", "模型提供的推理摘要：" + data.reasoning_summary));
    detail.append(node("h3", "", "执行步骤"));
    for (const event of data.events || []) {
      const box = node("details", "trace-event");
      const title = node("summary", event.status === "error" ? "trace-error" : "",
        "#" + (event.seq || "—") + " " + event.kind + " · " + event.name + " · " + event.status +
        (event.duration_ms === undefined ? "" : " · " + event.duration_ms + " ms"));
      const fields = { ...event };
      for (const key of ["seq", "at", "kind", "name", "status", "duration_ms"]) delete fields[key];
      box.append(title, node("pre", "", JSON.stringify(fields, null, 2)));
      detail.append(box);
    }
    for (const button of $("traceList").querySelectorAll("button")) {
      button.classList.toggle("active", button.textContent.includes(id.slice(0, 12)));
    }
  }

  async function connect(token) {
    state.connected = false;
    state.token = token; $("connection").textContent = "正在连接";
    try {
      const me = await api("/api/me");
      if (state.ownerId && state.ownerId !== me.user_id) {
        state.conversationId = ""; showMessages([]);
        state.selectedTrace = ""; state.prefill = null;
        $("traceList").replaceChildren(); $("traceDetail").replaceChildren();
      }
      state.ownerId = me.user_id;
      $("identity").textContent = me.username + " · " + me.user_id;
      $("conversationCaption").textContent = "历史对话 · user_id " + me.user_id;
      await Promise.all([loadConversations(), refreshTasks()]);
      state.connected = true;
      $("connection").textContent = "已连接";
      if (state.pendingSource) {
        const source = state.pendingSource; state.pendingSource = "";
        await openSource(source).catch((err) => notice(err.message, true));
      }
    } catch (err) { state.token = ""; $("connection").textContent = "连接失败"; notice(err.message, true); }
  }

  async function init() {
    try {
      const config = await fetch("/api/widget-config").then((res) => res.json());
      if (Array.isArray(config.allowed_origins) && config.allowed_origins.length) allowedOrigins = new Set(config.allowed_origins);
    } catch (_) { /* 默认只允许同源宿主。 */ }
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent || !allowedOrigins.has(event.origin)) return;
      if (event.data?.type === "aml-agent-token" && event.data.nonce === nonce && typeof event.data.token === "string") {
        state.hostOrigin = event.origin;
        connect(event.data.token);
      }
      if (event.data?.type === "aml-agent-open-source" && event.data.nonce === nonce && typeof event.data.sourceId === "string") {
        if (state.connected) openSource(event.data.sourceId).catch((err) => notice(err.message, true));
        else state.pendingSource = event.data.sourceId;
      }
      if (event.data?.type === "aml-agent-refresh") window.parent.postMessage({ type: "aml-agent-ready", nonce }, event.origin);
    });
    if (window.parent !== window) window.parent.postMessage({ type: "aml-agent-ready", nonce }, "*");
    else {
      await connect("");
    }
  }

  $("newConversation").onclick = () => newConversation().catch((err) => notice(err.message, true));
  $("refreshTasks").onclick = () => refreshTasks().catch((err) => notice(err.message, true));
  $("chatForm").onsubmit = sendMessage;
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (!state.busy) $("chatForm").requestSubmit();
    }
  });
  $("settingsButton").onclick = async () => {
    try {
      const values = await api("/api/settings");
      $("businessToken").value = ""; $("modelApiKey").value = "";
      $("businessToken").placeholder = values.business_token_configured ? "已保存，留空保持；勾选清除可删除" : "粘贴业务系统 Token";
      $("modelApiKey").placeholder = values.model_api_key_configured ? "已保存，留空保持" : "输入 API Key";
      $("clearBusinessToken").checked = false; $("clearModelApiKey").checked = false;
      $("modelName").value = values.model_name;
      $("modelBaseUrl").value = values.model_base_url;
      $("modelTimeout").value = values.model_timeout;
      $("modelMaxTokens").value = values.model_max_tokens;
      $("settingsDialog").showModal();
    } catch (err) { notice(err.message, true); }
  };
  $("closeSettings").onclick = () => $("settingsDialog").close();
  $("settingsForm").onsubmit = async (event) => {
    event.preventDefault();
    $("saveSettings").disabled = true;
    try {
      const values = { model_name: $("modelName").value, model_base_url: $("modelBaseUrl").value,
        model_timeout: Number($("modelTimeout").value), model_max_tokens: Number($("modelMaxTokens").value) };
      if ($("businessToken").value.trim()) values.business_token = $("businessToken").value.trim();
      if ($("modelApiKey").value.trim()) values.model_api_key = $("modelApiKey").value.trim();
      if ($("clearBusinessToken").checked) values.business_token = "";
      if ($("clearModelApiKey").checked) values.model_api_key = "";
      await api("/api/settings", { method: "PUT", body: JSON.stringify(values) });
      $("businessToken").value = ""; $("modelApiKey").value = "";
      $("settingsDialog").close();
      notice("配置已保存，新的模型配置将在下一条消息生效");
      await connect("");
    } catch (err) { notice(err.message, true); }
    finally { $("saveSettings").disabled = false; }
  };
  $("generatePrefill").onclick = generatePrefill;
  $("closeSource").onclick = () => $("sourceDialog").close();
  $("sourceDialog").addEventListener("close", () => {
    sourceLoad++;
    if (state.sourceUrl) URL.revokeObjectURL(state.sourceUrl);
    state.sourceUrl = ""; $("sourceContent").replaceChildren();
  });
  $("uploadButton").onclick = uploadMaterial;
  $("submitMaterial").onclick = submitMaterial;
  $("submitDecision").onclick = submitDecision;
  $("traceLink").onclick = () => {
    if (state.traceOpen) showChatView();
    else openTraceView().catch((err) => notice(err.message, true));
  };
  $("traceSearchForm").onsubmit = (event) => {
    event.preventDefault();
    state.selectedTrace = "";
    loadTraces($("traceQuery").value.trim()).catch((err) => notice(err.message, true));
  };
  init();
})();
