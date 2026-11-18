(function () {
  "use strict";
  // currentScript 只在脚本执行期间存在；先保存地址供宿主稍后调用 mount。
  var scriptUrl = document.currentScript && document.currentScript.src;
  /** 在宿主页面调用 AmlTaskAgent.mount(container, { baseUrl, token })。token 可为函数。 */
  window.AmlTaskAgent = {
    mount: function (container, options) {
      if (!container) throw new Error("Missing embed container");
      options = options || {};
      if (!options.baseUrl && !scriptUrl) throw new Error("Missing agent base URL");
      var base = new URL(options.baseUrl || scriptUrl.replace(/\/widget\/embed\.js.*$/, ""), location.href);
      var frame = document.createElement("iframe");
      frame.src = new URL("/widget/", base).href;
      frame.title = "AML 任务助手";
      frame.style.cssText = "width:100%;height:100%;min-height:620px;border:0;border-radius:12px;background:#fff";
      container.appendChild(frame);
      var currentNonce = "";
      var pendingSource = options.sourceId || "";
      var pendingTask = options.taskContext || null;
      function openTask(task) {
        pendingTask = task;
        if (currentNonce && task) {
          frame.contentWindow.postMessage({type: "aml-agent-open-task", nonce: currentNonce, task: task}, base.origin);
          pendingTask = null;
        }
      }
      function openSource(sourceId) {
        pendingSource = sourceId;
        if (currentNonce) {
          frame.contentWindow.postMessage({ type: "aml-agent-open-source", nonce: currentNonce, sourceId: sourceId }, base.origin);
          pendingSource = "";
        }
      }
      function sendToken(nonce) {
        var value = typeof options.token === "function" ? options.token() : options.token;
        Promise.resolve(value).then(function (token) {
          if (token && frame.contentWindow) frame.contentWindow.postMessage({ type: "aml-agent-token", nonce: nonce, token: token }, base.origin);
        });
      }
      function onMessage(event) {
        if (event.origin !== base.origin || event.source !== frame.contentWindow) return;
        if (event.data && event.data.type === "aml-agent-ready") {
          currentNonce = event.data.nonce;
          sendToken(currentNonce);
          if (pendingSource) openSource(pendingSource);
          if (pendingTask) openTask(pendingTask);
        }
        if (event.data && event.data.type === "aml-agent-prefill" && typeof options.onPrefill === "function") {
          options.onPrefill(event.data.prefill);
        }
        if (event.data && event.data.type === "aml-agent-handoff" && typeof options.onHandoff === "function") {
          options.onHandoff(event.data.handoff);
        }
      }
      window.addEventListener("message", onMessage);
      return { element: frame, openSource: openSource, openTask: openTask, refreshToken: function () { frame.contentWindow.postMessage({ type: "aml-agent-refresh" }, base.origin); },
        destroy: function () { window.removeEventListener("message", onMessage); frame.remove(); } };
    }
  };
})();
