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
  let taskEpoch = 0;
  const taskPaging = { risk: { page: 0, loaded: 0 }, bnf: { page: 0, loaded: 0 } };
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

  // Keep model output separate from metadata; never insert unsanitized model HTML.
  function renderAnswer(container, text) {
    const content = node("div", "markdown-body");
    if (window.marked && window.DOMPurify) {
      content.append(window.DOMPurify.sanitize(window.marked.parse(String(text || ""), {
        gfm: true, breaks: true
      }), {
        RETURN_DOM_FRAGMENT: true,
        ALLOWED_TAGS: ["p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del",
          "ul", "ol", "li", "blockquote", "pre", "code", "hr", "table", "thead", "tbody", "tr", "th", "td", "a"],
        ALLOWED_ATTR: ["href", "title", "start", "align"],
        ALLOW_DATA_ATTR: false
      }));
      for (const link of content.querySelectorAll("a[href]")) {
        const href = link.getAttribute("href");
        if (!/^(https?:|mailto:|\/|#)/i.test(href) || href.startsWith("//")) {
          link.removeAttribute("href");
        } else if (/^https?:/i.test(href)) {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        }
      }
      for (const table of content.querySelectorAll("table")) {
        const scroll = node("div", "markdown-table");
        scroll.tabIndex = 0;
        scroll.setAttribute("role", "region");
        scroll.setAttribute("aria-label", "结果表格，可横向滚动");
        table.replaceWith(scroll);
        scroll.append(table);
      }
    } else {
      content.textContent = text;
      content.classList.add("plain-text");
    }
    container.replaceChildren(content);
  }

  function empty(container, text) { container.replaceChildren(node("div", "muted", text)); }

  function fmtDuration(ms) {
    if (ms === null || ms === undefined) return "—";
    const value = Number(ms);
    if (!isFinite(value)) return "—";
    if (value < 1000) return Math.round(value) + " ms";
    return (value / 1000).toFixed(value < 10000 ? 2 : 1) + " s";
  }

  function fmtCompact(value) {
    if (value === null || value === undefined) return "—";
    const num = Number(value);
    if (!isFinite(num)) return "—";
    if (Math.abs(num) >= 1e6) return (num / 1e6).toFixed(1) + "M";
    if (Math.abs(num) >= 1e4) return (num / 1e3).toFixed(1) + "K";
    if (!Number.isInteger(num)) return num.toFixed(1);
    return num.toLocaleString("zh-CN");
  }

  async function loadConversations() {
    const result = await api("/api/conversations");
    const list = $("conversations"); list.replaceChildren();
    for (const item of result.records) {
      const button = node("button", "conversation-item" + (item.id === state.conversationId ? " active" : ""), item.title);
      button.title = item.title;
      button.dataset.conversationId = item.id;
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
        node("p", "", "可以问“我有多少待办？”或点击上方「我的待办」选择任务。"));
      list.append(emptyBox); return;
    }
    for (const msg of messages) {
      const bubble = node("div", "message " + msg.role);
      const turn = msg.role === "assistant" ? node("div", "assistant-turn") : null;
      if (turn) renderAnswer(bubble, msg.content);
      else bubble.textContent = msg.content;
      if (msg.trace_id && msg.role === "assistant") {
        const thinking = node("details", "live-process");
        thinking.append(node("summary", "", "模型思考 · 正在读取"));
        turn.append(thinking);
        api("/api/traces/" + encodeURIComponent(msg.trace_id)).then((trace) => {
          if (thinking.isConnected === false) return;
          thinking.replaceChildren(node("summary", "", "模型思考"));
          const models = (trace.events || []).filter((item) => item.kind === "model");
          const parts = models.map((item) => item.reasoning || item.reasoning_summary).filter(Boolean);
          thinking.append(node("pre", "reasoning-text", parts.join("\n\n") || "本轮模型服务未返回思考内容"));
        }).catch(() => { thinking.replaceChildren(node("summary", "", "模型思考记录暂不可用")); });
        const link = node("button", "message-meta text-button", "查看全链路 " + msg.trace_id.slice(0, 10));
        link.onclick = () => openTraceView(msg.trace_id).catch((err) => notice(err.message, true));
        bubble.append(link);
      }
      if (turn) { turn.append(bubble); list.append(turn); }
      else list.append(bubble);
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
      const selected = [...$("conversations").querySelectorAll("button")].find(button => button.dataset.conversationId === state.conversationId);
      if (selected && selected.textContent === "新对话") {
        const title = question.replace(/\s+/g, " ");
        selected.textContent = Array.from(title).length > 40 ? Array.from(title).slice(0, 39).join("") + "…" : title;
        selected.title = title;
      }
      $("question").value = "";
      const list = $("messages");
      if (list.querySelector(".empty-state")) list.replaceChildren();
      list.append(node("div", "message user", question));
      const wrap = node("div", "assistant-turn");
      const live = node("details", "live-process");
      live.open = true;
      const liveSummary = node("summary", "", "思考与执行过程");
      const liveBody = node("div", "live-body");
      live.append(liveSummary, liveBody);
      const bubble = node("div", "message assistant pending", "正在连接模型…");
      wrap.append(live, bubble);
      list.append(wrap);
      const done = await streamTurn(question, liveBody, bubble, list);
      renderAnswer(bubble, done.answer);
      bubble.classList.remove("pending");
      if (done.trace_id) {
        const traceId = done.trace_id;
        const link = node("button", "message-meta text-button", "查看全链路 " + traceId.slice(0, 10));
        link.onclick = () => openTraceView(traceId).catch((err) => notice(err.message, true));
        bubble.append(link);
      }
      if (!done.reasoning_available) liveBody.prepend(node("div", "muted", "本轮模型服务未返回思考内容"));
      live.open = true;
      liveSummary.textContent = "模型思考与工具执行 · 已完成";
      list.scrollTop = list.scrollHeight;
      await loadConversations();
    } catch (err) { notice(err.message + "；本轮未自动重发，可查看已收到的内容和调用链", true); }
    finally { state.busy = false; $("sendButton").disabled = false; }
  }

  function parseSseBlock(block) {
    let type = "message";
    const dataLines = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return null;
    try { return { type, data: JSON.parse(dataLines.join("\n")) }; } catch (_) { return null; }
  }

  async function streamTurn(question, liveBody, bubble, list) {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers["X-Access-Token"] = state.token;
    else if (window.parent === window) headers["X-Use-Saved-Token"] = "true";
    const response = await fetch("/api/conversations/" + state.conversationId + "/messages/stream", {
      method: "POST", headers, body: JSON.stringify({ question }), credentials: "omit"
    });
    const traceHeader = response.headers.get("X-Trace-Id");
    if (traceHeader) state.lastTrace = traceHeader;
    if (!response.ok) {
      let detail = "流式请求失败（HTTP " + response.status + "）";
      try {
        const data = await response.json();
        if (typeof data.detail === "string") detail = data.detail;
      } catch (_) { /* 保留默认错误文案 */ }
      throw new Error(detail);
    }
    if (!response.body) throw new Error("当前浏览器不支持流式响应");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const ui = { thinking: null, answer: "", pendingTools: [] };
    let buffer = "";
    let done = null;
    try {
      while (true) {
        const frame = await reader.read();
        if (frame.done) break;
        buffer += decoder.decode(frame.value, { stream: true });
        let index;
        while ((index = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          const parsed = parseSseBlock(block);
          if (!parsed) continue;
          if (parsed.type === "error") throw new Error(parsed.data.message || "模型执行失败");
          if (parsed.type === "done") { done = parsed.data; continue; }
          applyStreamEvent(parsed, ui, liveBody, bubble, list);
        }
      }
    } finally {
      if (!done) reader.cancel().catch(() => {});
      else reader.releaseLock();
    }
    if (!done) throw new Error("流式响应未完成");
    return done;
  }

  function applyStreamEvent(parsed, ui, liveBody, bubble, list) {
    const { type, data } = parsed;
    if (type === "thinking_delta") {
      if (!ui.thinking) {
        const box = node("div", "thinking-block");
        box.append(node("div", "thinking-caption", "思考中…"));
        ui.thinking = node("pre");
        box.append(ui.thinking);
        liveBody.append(box);
      }
      ui.thinking.textContent += data.text;
      ui.thinking.scrollTop = ui.thinking.scrollHeight;
    } else if (type === "answer_delta") {
      ui.thinking = null;
      ui.answer += data.text;
      renderAnswer(bubble, ui.answer);
      bubble.classList.remove("pending");
    } else if (type === "tool_call") {
      ui.thinking = null;
      const line = node("div", "tool-line");
      const args = typeof data.args === "string" ? data.args : JSON.stringify(data.args || {});
      line.append(node("span", "tool-icon", "🔧"),
        node("span", "tool-name", data.name || "tool"),
        node("span", "tool-args", args.length > 160 ? args.slice(0, 160) + "…" : args));
      const status = node("span", "tool-status", "调用中…");
      line.append(status);
      liveBody.append(line);
      ui.pendingTools.push({ name: data.name, callId: data.call_id, el: status });
    } else if (type === "tool_result") {
      ui.thinking = null;
      const index = ui.pendingTools.findIndex((item) => data.call_id ? item.callId === data.call_id : item.name === data.name);
      const pending = index >= 0 ? ui.pendingTools.splice(index, 1)[0] : null;
      const status = pending ? pending.el : null;
      if (status) {
        status.textContent = data.ok ? "✓ 完成" : "✕ 失败";
        status.className = "tool-status " + (data.ok ? "ok" : "error");
      } else {
        const line = node("div", "tool-line");
        line.append(node("span", "tool-icon", data.ok ? "✓" : "✕"),
          node("span", "tool-name", data.name || "tool"),
          node("span", "tool-status " + (data.ok ? "ok" : "error"), data.ok ? "完成" : "失败"));
        liveBody.append(line);
      }
      if (data.preview) status?.setAttribute("title", String(data.preview).slice(0, 400));
    }
    list.scrollTop = list.scrollHeight;
  }

  async function loadSummary() {
    const epoch = taskEpoch;
    try {
      const data = await api("/api/tasks/summary");
      if (epoch !== taskEpoch) return;
      $("taskTotal").textContent = data.total;
      $("taskBadge").textContent = data.total;
      $("taskTypes").textContent = data.by_type.length + " 类流程待处理";
      const types = $("taskTypeList"); types.replaceChildren();
      for (const item of data.by_type) {
        const row = node("div", "task-type-row");
        row.append(node("span", "", item.name || item.process_type), node("strong", "", String(item.count)));
        types.append(row);
      }
    } catch (err) {
      if (epoch !== taskEpoch) return;
      $("taskTotal").textContent = "—"; $("taskBadge").textContent = "—";
      $("taskTypes").textContent = "待办数量读取失败";
      empty($("taskTypeList"), err.message);
    }
  }

  async function loadRiskTasks(append = false) {
    const epoch = taskEpoch;
    const page = append ? taskPaging.risk.page + 1 : 1;
    const list = $("riskTasks");
    $("riskMore").disabled = true;
    try {
      const data = await api("/api/tasks/risk?page=" + page + "&page_size=20");
      if (epoch !== taskEpoch) return;
      if (!append) list.replaceChildren();
      taskPaging.risk = { page, loaded: (append ? taskPaging.risk.loaded : 0) + data.records.length };
      $("riskCount").textContent = data.total;
      $("riskMore").classList.toggle("hidden", page * 20 >= data.total || !data.records.length);
      $("riskMore").textContent = "加载更多（已显示 " + taskPaging.risk.loaded + " / " + data.total + "）";
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
      if (!append && !data.records.length) empty(list, "暂无风险待办");
    } catch (err) {
      if (epoch !== taskEpoch) return;
      if (append) notice(err.message, true);
      else { $("riskCount").textContent = "不可用"; empty(list, "风险列表不可用：" + err.message); }
    } finally { if (epoch === taskEpoch) $("riskMore").disabled = false; }
  }

  async function loadBnfTasks(append = false) {
    const epoch = taskEpoch;
    const page = append ? taskPaging.bnf.page + 1 : 1;
    const list = $("bnfTasks");
    $("bnfMore").disabled = true;
    try {
      const data = await api("/api/tasks/bnf?page=" + page + "&page_size=20");
      if (epoch !== taskEpoch) return;
      if (!append) list.replaceChildren();
      taskPaging.bnf = { page, loaded: (append ? taskPaging.bnf.loaded : 0) + data.records.length };
      $("bnfCount").textContent = data.total;
      $("bnfMore").classList.toggle("hidden", page * 20 >= data.total || !data.records.length);
      $("bnfMore").textContent = "加载更多（已显示 " + taskPaging.bnf.loaded + " / " + data.total + "）";
      for (const item of data.records) {
        const card = node("div", "task-card");
        card.append(node("div", "task-card-title", item.task_name + " · " + item.task_id));
        card.append(node("div", "task-card-meta", item.reason));
        const button = node("button", "mini-button", "发起补材料任务");
        button.onclick = () => startReview("bnfValidate", item.task_id, item.act_id);
        card.append(button); list.append(card);
      }
      if (!append && !data.records.length) empty(list, "暂无受益所有人核验待办");
    } catch (err) {
      if (epoch !== taskEpoch) return;
      if (append) notice(err.message, true);
      else { $("bnfCount").textContent = "不可用"; empty(list, "核验列表不可用：" + err.message); }
    } finally { if (epoch === taskEpoch) $("bnfMore").disabled = false; }
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
    const epoch = taskEpoch;
    const data = await api("/api/material-tasks");
    if (epoch !== taskEpoch) return;
    $("materialCount").textContent = data.records.length;
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
    const epoch = taskEpoch;
    const data = await api("/api/workflows");
    if (epoch !== taskEpoch) return;
    $("workflowCount").textContent = data.records.length;
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
    const epoch = ++taskEpoch;
    $("riskMore").classList.add("hidden"); $("bnfMore").classList.add("hidden");
    const results = await Promise.allSettled([loadSummary(), loadRiskTasks(), loadBnfTasks(), loadMaterialTasks(), loadWorkflows()]);
    if (epoch !== taskEpoch) return;
    for (const [index, countId, listId] of [[3, "materialCount", "materialTasks"], [4, "workflowCount", "workflows"]]) {
      if (results[index].status === "rejected") {
        $(countId).textContent = "不可用";
        empty($(listId), "列表读取失败：" + results[index].reason.message);
      }
    }
  }

  function showChatView() {
    $("automationView").classList.add("hidden");
    state.traceOpen = false;
    $("chatView").classList.remove("hidden");
    $("traceView").classList.add("hidden");
    $("traceLink").textContent = "调用链";
  }

  async function openTraceView(traceId = "") {
    $("automationView").classList.add("hidden");
    state.traceOpen = true;
    $("chatView").classList.add("hidden");
    $("traceView").classList.remove("hidden");
    $("traceLink").textContent = "返回对话";
    switchTraceTab(false);
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
      const usage = item.total_tokens || {};
      const metrics = [item.trace_id.slice(0, 12), item.status || "ok"];
      if (item.duration_ms !== null && item.duration_ms !== undefined) metrics.push(fmtDuration(item.duration_ms));
      if (usage.total) metrics.push(fmtCompact(usage.total) + " tok");
      button.append(node("strong", "", item.name || "调用链"),
        node("span", "", metrics.join(" · ")),
        node("div", "muted", new Date(item.created_at * 1000).toLocaleString()));
      button.onclick = () => selectTrace(item.trace_id).catch((err) => notice(err.message, true));
      list.append(button);
    }
    if (!records.length) empty(list, "没有找到当前用户的调用链");
    if (exact && records.length) await selectTrace(query);
    else if (!state.selectedTrace && records.length) await selectTrace(records[0].trace_id);
  }

  const KIND_LABEL = { api: "请求", operation: "处理", agent: "Agent", model: "模型调用", tool: "工具调用",
    tool_failure: "工具失败", backend: "后端调用", backend_attempt: "后端尝试", auth: "身份认证",
    policy: "策略校验", decision: "决策" };

  function pretty(value) {
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }

  function formatMessages(messages) {
    if (!Array.isArray(messages)) return pretty(messages);
    return messages.map((item) => "[" + (item && item.role ? item.role : "?") + "] " +
      (item && typeof item.content === "string" ? item.content : pretty(item && item.content)) +
      (item?.tool_calls ? "\n工具调用：" + pretty(item.tool_calls) : "") +
      (item?.tool_call_id ? "\n工具调用 ID：" + item.tool_call_id : "") +
      (item?.truncated ? "\n[内容超过保存上限，已截断]" : "")).join("\n\n");
  }

  function statusBadge(status) {
    const text = status === "error" ? "✕ 失败" : status === "degraded" ? "⚠ 降级" : "✓ 成功";
    return node("span", "status-badge " + (status || "ok"), text);
  }

  function tile(label, value, valueClass, sub) {
    const box = node("div", "tile");
    box.append(node("span", "tile-label", label),
      node("span", "tile-value" + (valueClass ? " " + valueClass : ""), String(value)));
    if (sub) box.append(node("span", "tile-sub", sub));
    return box;
  }

  function eventSection(label, content, textClass) {
    const box = node("details", "event-section");
    box.open = label.includes("思考") || label === "模型推理摘要" || label === "模型输出";
    box.append(node("summary", "", label), node("pre", textClass || "", pretty(content)));
    return box;
  }

  function kvRow(pairs) {
    const row = node("div", "event-kv");
    for (const [label, value] of pairs) {
      const item = node("span");
      item.append(node("b", "", label + " "), document.createTextNode(String(value)));
      row.append(item);
    }
    return row;
  }

  function summaryBox(label, text) {
    return node("div", "trace-summary", label + "：" + text);
  }

  async function selectTrace(id) {
    state.selectedTrace = id;
    const data = await api("/api/traces/" + encodeURIComponent(id));
    renderTraceDetail(data, $("traceDetail"));
    for (const button of $("traceList").querySelectorAll("button")) {
      button.classList.toggle("active", button.textContent.includes(id.slice(0, 12)));
    }
  }

  function renderTraceDetail(data, detail) {
    detail.replaceChildren();
    const titleRow = node("div", "trace-meta");
    titleRow.append(statusBadge(data.status));
    titleRow.append(document.createTextNode("  " + data.trace_id + " · " + new Date(data.created_at * 1000).toLocaleString()));
    detail.append(node("h2", "", data.name || "调用链"), titleRow);
    if (data.conversation_id) detail.append(node("div", "trace-meta", "会话: " + data.conversation_id));
    if (data.workflow_id) detail.append(node("div", "trace-meta", "工作流: " + data.workflow_id));

    const events = data.events || [];
    const usage = data.total_tokens || {};
    const countKind = (kind) => events.filter((item) => item.kind === kind).length;
    const errorEvents = data.error_count ?? events.filter((item) => item.status === "error").length;
    const tiles = node("div", "trace-tiles");
    tiles.append(
      tile("总耗时", fmtDuration(data.duration_ms)),
      tile("模型累计耗时", fmtDuration(data.model_duration_ms ?? events.filter(e => e.kind === "model").reduce((sum, e) => sum + (e.duration_ms || 0), 0))),
      tile("Token", fmtCompact(usage.total), "", "输入 " + fmtCompact(usage.input) + " · 输出 " + fmtCompact(usage.output)),
      tile("模型调用", data.model_calls ?? countKind("model")),
      tile("工具调用", data.tool_calls ?? countKind("tool")),
      tile("错误事件", errorEvents, errorEvents > 0 ? "status-error" : ""));
    detail.append(tiles);

    if (data.dropped_events) detail.append(node("div", "trace-meta trace-error", "事件达到保存上限，较早的 " + data.dropped_events + " 条已移除"));
    // 结论与推理摘要只在这里展示一次；时间线会跳过重复的 decision 事件。
    const finalModel = [...events].reverse().find(item => item.kind === "model" && item.visible_output);
    if (data.decision_summary && (!finalModel || data.decision_summary !== finalModel.visible_output)) detail.append(summaryBox("最终回复", data.decision_summary));
    const hasModelReasoning = events.some((item) => item.kind === "model" && (item.reasoning || item.reasoning_summary));
    if (data.reasoning_summary && !hasModelReasoning) detail.append(summaryBox("模型推理摘要", data.reasoning_summary));

    detail.append(node("h3", "", "模型与工具调用时间线"));
    detail.append(renderTimeline(events, data));
  }

  function renderTimeline(events, data) {
    const box = node("div");
    const seen = new Set();
    const ordered = events.filter(event => ["model", "tool", "tool_failure"].includes(event.kind) || event.name === "turn_failed")
      .sort((a, b) => (a.started_at ?? a.at) - (b.started_at ?? b.at));
    for (const event of ordered) {
      if (event.run_id && seen.has(event.run_id)) continue;
      if (event.run_id) seen.add(event.run_id);
      if (event.kind === "tool_failure" && events.some(item => item.kind === "tool" && item.name === event.name && item.status === "error")) continue;
      box.append(renderEvent(event));
    }
    if (!ordered.length) box.append(node("p", "muted", "本轮尚无模型调用记录，请查看执行状态。"));
    return box;
  }

  function renderEvent(event) {
    const box = node("details", "trace-event" + (event.status === "error" ? " trace-event-error" : ""));
    const parts = ["#" + (event.seq ?? "—"), KIND_LABEL[event.kind] || event.kind, event.name];
    if (event.duration_ms !== undefined) parts.push(fmtDuration(event.duration_ms));
    if (event.token_usage) parts.push("tok " + event.token_usage.input_tokens + "→" + event.token_usage.output_tokens);
    box.append(node("summary", event.status === "error" ? "trace-error" : "",
      parts.join(" · ") + (event.status === "error" ? " · ✕ 失败" : "")));

    const body = node("div", "event-body");
    const rendered = new Set(["seq", "at", "kind", "name", "status", "duration_ms", "run_id", "parent_run_id", "started_at"]);
    if (event.kind === "model") {
      box.open = true;
      // 思考过程优先展示逐片累积的 reasoning；输入消息用于核对上下文与工具选择。
      if (event.reasoning) body.append(eventSection("思考过程", event.reasoning, "reasoning-text"));
      else if (event.reasoning_summary) body.append(eventSection("模型推理摘要", event.reasoning_summary, "reasoning-text"));
      else body.append(node("p", "muted", "模型服务未返回思考内容"));
      if (event.input_messages) {
        const total = event.input_message_count ?? event.input_messages.length;
        const section = node("details", "event-section");
        section.append(node("summary", "", "模型输入（共 " + total + " 条；超过保存上限会标明截断）"),
          node("pre", "", formatMessages(event.input_messages)));
        body.append(section);
      }
      if (event.visible_output !== undefined && event.visible_output !== "") body.append(eventSection("模型输出", event.visible_output));
      if (event.tool_calls?.length) body.append(eventSection("模型选择的工具与参数", event.tool_calls));
      if (event.invalid_tool_calls?.length) body.append(eventSection("工具参数解析失败", event.invalid_tool_calls));
      if (event.available_tools?.length) body.append(eventSection("本次可选工具及参数约束", event.available_tools));
      body.append(kvRow([["首个模型分片", fmtDuration(event.first_token_ms)], ["结束原因", event.finish_reason || "未返回"]]));
      if (event.output_tokens_per_second !== undefined) body.append(kvRow([["平均输出速度", event.output_tokens_per_second + " token/秒"]]));
      if (event.usage_details && Object.keys(event.usage_details).length) body.append(eventSection("Token 明细（含提供方返回的缓存、推理用量）", event.usage_details));
      if (event.usage_available === false) body.append(node("p", "muted", "模型服务未返回 Token 用量"));
      if (event.token_usage) {
        const item = event.token_usage;
        body.append(kvRow([["Token", item.input_tokens + " 输入 / " + item.output_tokens + " 输出 / " + item.total_tokens + " 合计"]]));
      }
      for (const key of ["reasoning", "reasoning_summary", "input_messages", "input_message_count",
        "visible_output", "token_usage", "tool_calls", "invalid_tool_calls", "available_tools", "first_token_ms", "finish_reason", "reasoning_available", "usage_details", "usage_available", "output_tokens_per_second", "started_at"]) rendered.add(key);
    } else if (event.kind === "tool") {
      box.open = true;
      if (event.model_run_id) body.append(node("div", "trace-meta", "来自模型调用 " + event.model_run_id));
      if (event.tool_call_id) body.append(node("div", "trace-meta", "工具调用 ID：" + event.tool_call_id));
      if (event.input !== undefined) body.append(eventSection("输入参数", event.input));
      if (event.output !== undefined) body.append(eventSection("输出结果", event.output));
      rendered.add("input"); rendered.add("output");
      rendered.add("model_run_id"); rendered.add("tool_call_id");
    }
    const rest = {};
    for (const key of Object.keys(event)) if (!rendered.has(key)) rest[key] = event[key];
    if (Object.keys(rest).length) body.append(eventSection("原始字段", rest));
    box.append(body);
    return box;
  }

  function switchTraceTab(showDashboard) {
    $("tabTraces").classList.toggle("active", !showDashboard);
    $("tabDashboard").classList.toggle("active", showDashboard);
    $("tabTraces").setAttribute("aria-selected", String(!showDashboard));
    $("tabDashboard").setAttribute("aria-selected", String(showDashboard));
    $("tracePane").classList.toggle("hidden", showDashboard);
    $("dashboardPane").classList.toggle("hidden", !showDashboard);
    $("traceSearchForm").classList.toggle("hidden", showDashboard);
    if (showDashboard) loadDashboard().catch((err) => notice(err.message, true));
  }

  async function loadDashboard() {
    const params = new URLSearchParams();
    if ($("dashScope").value === "conversation" && state.conversationId) params.set("conversation_id", state.conversationId);
    const range = Number($("dashRange").value);
    if (range > 0) params.set("since", String(Math.round(Date.now() / 1000 - range)));
    if ($("dashStatus").value) params.set("status", $("dashStatus").value);
    if ($("dashName").value.trim()) params.set("name", $("dashName").value.trim());
    params.set("limit", "100");
    const data = await api("/api/traces/dashboard?" + params.toString());
    $("dashDetail").replaceChildren();
    renderDashTiles(data.aggregates || {});
    renderDashTable(data.records || []);
  }

  function renderDashTiles(agg) {
    const tiles = $("dashTiles"); tiles.replaceChildren();
    const tokens = agg.total_tokens || {};
    const errorRate = agg.count ? ((agg.error_rate || 0) * 100).toFixed(1) + "%" : "0%";
    tiles.append(
      tile("链路数", fmtCompact(agg.count), "",
        (agg.error_trace_count || 0) + " 条失败 · " + (agg.degraded_trace_count || 0) + " 条降级"),
      tile("错误率", errorRate, (agg.error_rate || 0) > 0 ? "status-error" : "status-ok",
        "错误事件 " + fmtCompact(agg.error_event_count)),
      tile("平均耗时", fmtDuration(agg.avg_duration_ms), "", "最长 " + fmtDuration(agg.max_duration_ms)),
      tile("P50 / P95 耗时", fmtDuration(agg.p50_duration_ms) + " / " + fmtDuration(agg.p95_duration_ms),
        "", "按筛选范围内链路统计"),
      tile("Token 总量", fmtCompact(tokens.total), "", "输入 " + fmtCompact(tokens.input) + " · 输出 " + fmtCompact(tokens.output)),
      tile("平均 Token", fmtCompact(agg.avg_tokens), "", "每链路"),
      tile("模型调用", fmtCompact(agg.model_calls)),
      tile("工具调用", fmtCompact(agg.tool_calls)));
  }

  function renderDashTable(records) {
    const box = $("dashTable"); box.replaceChildren();
    if (!records.length) { box.append(node("p", "muted", "当前筛选条件下没有链路记录")); return; }
    const table = node("table");
    const head = node("tr");
    for (const [label, cls] of [["时间", ""], ["链路", ""], ["状态", ""], ["耗时", "num"],
      ["Token", "num"], ["模型", "num"], ["工具", "num"], ["Trace ID", ""]]) head.append(node("th", cls, label));
    table.append(head);
    for (const item of records) {
      const row = node("tr");
      const nameCell = node("td", "", item.name || "调用链");
      nameCell.title = item.name || "";
      const statusCell = node("td"); statusCell.append(statusBadge(item.status));
      const openCell = node("td");
      const open = node("button", "trace-open", item.trace_id.slice(0, 12));
      open.type = "button";
      open.onclick = () => {
        $("dashDetail").textContent = "正在加载本轮模型输入输出…";
        api("/api/traces/" + item.trace_id).then(data => renderTraceDetail(data, $("dashDetail")))
          .catch(err => notice(err.message, true));
      };
      openCell.append(open);
      const tokens = item.total_tokens || {};
      row.append(node("td", "", new Date(item.created_at * 1000).toLocaleString()), nameCell, statusCell,
        node("td", "num", fmtDuration(item.duration_ms)),
        node("td", "num", fmtCompact(tokens.total)),
        node("td", "num", String(item.model_calls ?? 0)),
        node("td", "num", String(item.tool_calls ?? 0)),
        openCell);
      table.append(row);
    }
    box.append(table);
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
        $("dashTiles").replaceChildren(); $("dashTable").replaceChildren();
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
  function setSidebarCollapsed(collapsed) {
    $("app").classList.toggle("sidebar-collapsed", collapsed);
    $("sidebar").inert = collapsed;
    $("sidebar").setAttribute("aria-hidden", String(collapsed));
    $("toggleSidebar").setAttribute("aria-expanded", String(!collapsed));
    if (collapsed) $("toggleSidebar").focus();
    try { window.localStorage.setItem("aml-sidebar-collapsed", String(collapsed)); } catch (_) { /* 存储受限时仅保留本次页面状态。 */ }
  }
  $("toggleSidebar").onclick = () => setSidebarCollapsed(!$("app").classList.contains("sidebar-collapsed"));
  $("collapseSidebar").onclick = () => setSidebarCollapsed(true);
  $("tasksButton").onclick = () => $("taskDrawer").showModal();
  $("closeTasks").onclick = () => $("taskDrawer").close();
  $("expandTasks").onclick = () => {
    const expanded = !$("taskDrawer").classList.contains("drawer-expanded");
    $("taskDrawer").classList.toggle("drawer-expanded", expanded);
    $("expandTasks").textContent = expanded ? "恢复宽度" : "加宽抽屉";
    $("expandTasks").setAttribute("aria-pressed", String(expanded));
  };
  $("taskDrawer").addEventListener("click", event => { if (event.target === $("taskDrawer")) $("taskDrawer").close(); });
  $("riskMore").onclick = () => loadRiskTasks(true);
  $("bnfMore").onclick = () => loadBnfTasks(true);
  let initiallyCollapsed = Boolean(window.matchMedia?.("(max-width:760px)").matches);
  try {
    const saved = window.localStorage.getItem("aml-sidebar-collapsed");
    if (saved !== null) initiallyCollapsed = saved === "true";
  } catch (_) { /* 独立页面与嵌入页面均可在无本地存储时使用。 */ }
  if (initiallyCollapsed) setSidebarCollapsed(true);
  $("chatForm").onsubmit = sendMessage;
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (!state.busy) $("chatForm").requestSubmit();
    }
  });
  async function loadSettings() {
    $("settingsFields").disabled = true;
    $("settingsStatus").textContent = "正在读取配置…";
    try {
      const values = await api("/api/settings");
      $("amlBaseUrl").value = values.aml_base_url;
      $("schedulerAmlToken").value = "";
      $("schedulerAmlToken").placeholder = values.scheduler_aml_token_configured ? "已保存，留空保持" : "输入调度专用账号 Token";
      $("clearSchedulerAmlToken").checked = false;
      $("businessToken").value = ""; $("modelApiKey").value = "";
      $("businessToken").placeholder = values.business_token_configured ? "已保存，留空保持；勾选清除可删除" : "粘贴业务系统 Token";
      $("modelApiKey").placeholder = values.model_api_key_configured ? "已保存，留空保持" : "输入 API Key";
      $("clearBusinessToken").checked = false; $("clearModelApiKey").checked = false;
      $("modelName").value = values.model_name;
      $("modelBaseUrl").value = values.model_base_url;
      $("modelTimeout").value = values.model_timeout;
      $("modelMaxTokens").value = values.model_max_tokens;
      $("settingsFields").disabled = false;
      $("settingsStatus").textContent = "";
    } catch (err) { $("settingsStatus").textContent = "配置读取失败：" + err.message; }
  }
  $("settingsButton").onclick = () => {
    $("settingsDialog").showModal();
    return loadSettings();
  };
  $("retrySettings").onclick = loadSettings;
  $("closeSettings").onclick = () => $("settingsDialog").close();
  $("settingsForm").onsubmit = async (event) => {
    event.preventDefault();
    $("saveSettings").disabled = true;
    try {
      const values = { aml_base_url: $("amlBaseUrl").value,
        model_timeout: Number($("modelTimeout").value), model_max_tokens: Number($("modelMaxTokens").value) };
      if ($("modelName").value.trim()) values.model_name = $("modelName").value.trim();
      if ($("modelBaseUrl").value.trim()) values.model_base_url = $("modelBaseUrl").value.trim();
      if ($("schedulerAmlToken").value.trim()) values.scheduler_aml_token = $("schedulerAmlToken").value.trim();
      if ($("clearSchedulerAmlToken").checked) values.scheduler_aml_token = "";
      if ($("businessToken").value.trim()) values.business_token = $("businessToken").value.trim();
      if ($("modelApiKey").value.trim()) values.model_api_key = $("modelApiKey").value.trim();
      if ($("clearBusinessToken").checked) values.business_token = "";
      if ($("clearModelApiKey").checked) values.model_api_key = "";
      await api("/api/settings", { method: "PUT", body: JSON.stringify(values) });
      $("businessToken").value = ""; $("modelApiKey").value = "";
      $("schedulerAmlToken").value = "";
      $("settingsDialog").close();
      notice("系统配置已保存");
      await connect("");
    } catch (err) { notice(err.message, true); }
    finally { $("saveSettings").disabled = false; }
  };
  function renderAutomation(values) {
    $("autoSubmitEnabled").checked = values.auto_submit_enabled;
    $("autoSubmitInterval").value = values.auto_submit_interval_seconds;
    $("maxAutoSubmitPerRun").value = values.max_auto_submit_per_run;
    $("automationStatus").textContent = values.auto_submit_enabled
      ? "定时扫描已开启，周期 " + values.auto_submit_interval_seconds + " 秒，每轮最多提交 " + values.max_auto_submit_per_run + " 条"
      : "定时扫描已关闭";
  }
  async function loadAutomation() {
    $("automationFields").disabled = true;
    $("automationStatus").textContent = "正在读取扫描配置…";
    try {
      renderAutomation(await api("/api/settings"));
      $("automationFields").disabled = false;
    } catch (err) { $("automationStatus").textContent = "扫描配置读取失败：" + err.message; }
  }
  $("automationLink").onclick = () => {
    showChatView();
    $("chatView").classList.add("hidden");
    $("automationView").classList.remove("hidden");
    return loadAutomation();
  };
  $("closeAutomation").onclick = showChatView;
  $("retryAutomation").onclick = loadAutomation;
  $("automationForm").onsubmit = async (event) => {
    event.preventDefault();
    $("saveAutomation").disabled = true;
    try {
      const values = await api("/api/settings", { method: "PUT", body: JSON.stringify({
        auto_submit_enabled: $("autoSubmitEnabled").checked,
        auto_submit_interval_seconds: Number($("autoSubmitInterval").value),
        max_auto_submit_per_run: Number($("maxAutoSubmitPerRun").value)
      }) });
      renderAutomation(values);
      notice("扫描配置已保存");
    } catch (err) { $("automationStatus").textContent = "保存失败：" + err.message; }
    finally { $("saveAutomation").disabled = false; }
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
  $("tabTraces").onclick = () => switchTraceTab(false);
  $("tabDashboard").onclick = () => switchTraceTab(true);
  $("dashRefresh").onclick = () => loadDashboard().catch((err) => notice(err.message, true));
  $("dashName").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadDashboard().catch((err) => notice(err.message, true));
    }
  });
  init();
})();
