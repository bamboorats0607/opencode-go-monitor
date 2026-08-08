// OpenCode GO Cookie 捕获 — popup 逻辑
// v1.2: 每步埋点上报到程序 /debug 端点（用于排查 cookies API 卡住问题）
const RECEIVER = "http://127.0.0.1:8900/capture";
const DEBUG_URL = "http://127.0.0.1:8900/debug";
const COOKIE_NAME = "auth";
const COOKIE_URL = "https://opencode.ai/";
const TIMEOUT_MS = 5000;

const statusEl = document.getElementById("status");
const btn = document.getElementById("capture");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

// 埋点：POST 到程序 /debug，进入程序日志（frozen 时在 exe 目录 logs/）
function trace(msg) {
  try {
    fetch(DEBUG_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg: msg }),
    }).catch(() => {});
  } catch (e) {}
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(label + " 超时(" + ms + "ms)")), ms)),
  ]);
}

btn.addEventListener("click", async () => {
  btn.disabled = true;
  setStatus("正在读取 opencode.ai 的 auth cookie…");
  trace("click: start, typeof chrome=" + typeof chrome + ", chrome.cookies=" +
        (chrome && chrome.cookies ? "object" : "undefined"));
  try {
    // 环境自检
    if (!chrome || !chrome.cookies) {
      trace("FAIL: chrome.cookies 不可用（权限未生效？）");
      setStatus("环境异常：chrome.cookies 不可用。\n请重新加载扩展（edge://extensions → 刷新），确保已启用「cookies」权限。", "err");
      return;
    }
    trace("call cookies.get url=" + COOKIE_URL + " name=" + COOKIE_NAME);
    let cookie = null;
    try {
      cookie = await withTimeout(
        chrome.cookies.get({ url: COOKIE_URL, name: COOKIE_NAME }),
        TIMEOUT_MS, "cookies.get"
      );
      trace("cookies.get resolved: " + (cookie ? "cookie.len=" + String(cookie.value).length : "null(未找到)"));
    } catch (e) {
      trace("cookies.get FAILED: " + e.message);
      // 诊断：列出该域下实际 cookie 名
      let diag = "";
      try {
        const all = await withTimeout(
          chrome.cookies.getAll({ domain: "opencode.ai" }),
          TIMEOUT_MS, "cookies.getAll"
        );
        diag = "该域下 cookie 名：" + (all.map(c => c.name).join(", ") || "（无）");
        trace("getAll ok: " + diag);
      } catch (e2) {
        diag = "cookies.getAll 也失败：" + e2.message;
        trace("getAll FAILED: " + e2.message);
      }
      setStatus("读取失败：" + e.message + "\n" + diag +
                "\n请确认：① 已登录 opencode.ai ② 访问过一次 https://opencode.ai/ ③ 扩展已重新加载（edge://extensions → 刷新）", "err");
      return;
    }

    if (!cookie) {
      trace("FAIL: 未找到 auth cookie");
      setStatus("未找到名为 " + COOKIE_NAME + " 的 cookie。\n" +
                "请先在浏览器登录 opencode.ai 并访问一次\nhttps://opencode.ai/ 再点「捕获」按钮。", "err");
      return;
    }
    trace("POST cookie to /capture");
    const resp = await fetch(RECEIVER, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie: cookie.value }),
    });
    const data = await resp.json();
    trace("capture resp: ok=" + resp.ok + " data=" + JSON.stringify(data));
    if (resp.ok && data.ok) {
      setStatus("✓ 已发送 Cookie 给监控程序。\n程序正在拉取 Go 用量…", "ok");
    } else {
      setStatus("程序接收失败：" + (data.error || resp.status) + "\n请确认监控程序已运行。", "err");
    }
  } catch (e) {
    trace("FATAL: " + e.message);
    setStatus("发送失败：" + e.message + "\n请确认监控程序已运行（接收端 127.0.0.1:8900）。", "err");
  } finally {
    btn.disabled = false;
  }
});
