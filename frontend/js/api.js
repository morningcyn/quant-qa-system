// fetch 封装：JSON、超时、统一错误 toast
(function () {
  "use strict";

  const DEFAULT_TIMEOUT = 200000; // 质检接口最长 200s

  function toast(message, type) {
    if (window.UI && window.UI.toast) window.UI.toast(message, type);
    else console.error(message);
  }

  async function request(method, path, body, timeoutMs) {
    const options = { method, headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || DEFAULT_TIMEOUT);
    options.signal = controller.signal;
    let resp;
    try {
      resp = await fetch(path, options);
    } catch (err) {
      clearTimeout(timer);
      if (err.name === "AbortError") throw { code: "timeout", message: "请求超时，请检查网络或稍后重试" };
      throw { code: "network", message: "网络异常，无法连接本地服务" };
    }
    clearTimeout(timer);
    let data = null;
    try { data = await resp.json(); } catch (e) { /* 204 无内容 */ }
    if (!resp.ok) {
      const err = { code: (data && data.code) || "error", message: (data && data.message) || "请求失败", status: resp.status };
      throw err;
    }
    return data;
  }

  window.API = {
    get: (path, timeoutMs) => request("GET", path, undefined, timeoutMs),
    post: (path, body, timeoutMs) => request("POST", path, body, timeoutMs),
    put: (path, body) => request("PUT", path, body),
    del: (path) => request("DELETE", path),
  };
})();
