#!/usr/bin/env node
// Lay the site out in headless Chrome at an exact viewport and evaluate a JS
// expression against it — the way to check a layout, because a
// --window-size screenshot cannot go below ~500px wide and cannot tell you a
// single coordinate. Drives Chrome over the DevTools protocol with nothing
// but Node's built-in WebSocket.
//
//   node tools/measure.mjs <url> <WxH> '<expression>' [screenshot.png]
//
// The expression may return a promise; its value is printed as JSON. The
// page is loaded with prefers-reduced-motion so the intro sequence is
// skipped and the canvas is a static frame — layouts are identical either
// way, screenshots are just deterministic.
//
// MEASURE_HOLD=<substring> leaves every request whose URL contains it
// pending forever (e.g. MEASURE_HOLD=assets/shot-), which is what a not-yet-
// loaded lazy image looks like — measure with and without to prove a layout
// does not jump when the images arrive. (Blocking the requests instead would
// make them FAIL, and a failed <img> renders its alt text at a different
// size, which is not the case being tested.)
//
// Exits non-zero, with Chrome cleaned up, on any failure: no DevTools port,
// a navigation that fails (a dead server lands on chrome-error://, which
// used to be measured as if it were the page), or an expression that throws.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [url, size, expr, shot] = process.argv.slice(2);
if (!url || !size || !expr) {
  console.error("usage: measure.mjs <url> <WxH> '<expression>' [screenshot.png]");
  process.exit(2);
}
const [width, height] = size.split("x").map(Number);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const profile = mkdtempSync(join(tmpdir(), "measure-"));
const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
  `--user-data-dir=${profile}`, "--remote-debugging-port=0", "about:blank",
], { stdio: "ignore", detached: true });   // own process group: see the kill below

let ws;
try {
  // Chrome writes the port file non-atomically: wait for a non-empty first line.
  const portFile = join(profile, "DevToolsActivePort");
  let port = "";
  for (let i = 0; i < 200 && !port; i++) {
    if (existsSync(portFile)) port = readFileSync(portFile, "utf8").split("\n")[0].trim();
    if (!port) await sleep(50);
  }
  if (!port) throw new Error("Chrome did not open a DevTools port within 10s");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("no page target in Chrome");

  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = () => reject(new Error("DevTools socket failed")); });
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    else if (msg.method) events.push(msg.method);
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const n = ++id;
    pending.set(n, (msg) => (msg.error ? reject(new Error(`${method}: ${msg.error.message}`)) : resolve(msg.result)));
    ws.send(JSON.stringify({ id: n, method, params }));
  });
  const evaluate = async (expression) => {
    const { result, exceptionDetails } = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || exceptionDetails.text || "evaluate failed");
    return result.value;
  };

  await send("Page.enable");
  if (process.env.MEASURE_HOLD) {
    // Paused requests are simply never continued. MEASURE_HOLD_STAGE=Request
    // pauses before the request leaves; the default holds the response, i.e.
    // the request went out and no bytes came back.
    const requestStage = process.env.MEASURE_HOLD_STAGE === "Request" ? "Request" : "Response";
    await send("Fetch.enable", { patterns: [{ urlPattern: `*${process.env.MEASURE_HOLD}*`, requestStage }] });
  }
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 768 });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });

  const nav = await send("Page.navigate", { url });
  if (nav.errorText) throw new Error(`navigation failed: ${nav.errorText} (${url})`);
  let loaded = false;
  for (let i = 0; i < 200 && !loaded; i++) { loaded = events.includes("Page.loadEventFired"); if (!loaded) await sleep(50); }
  if (!loaded) console.error("measure.mjs: warning — no load event within 10s, measuring the page as it is");
  const href = await evaluate("location.href");
  if (href.startsWith("chrome-error://")) throw new Error(`page failed to load: ${url}`);

  await evaluate("document.fonts.ready.then(() => true)");
  await sleep(300);                                     // one more layout pass after fonts
  const value = await evaluate(`(async () => (${expr}))()`);
  console.log(JSON.stringify(value, null, 2));
  if (shot) {
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(shot, Buffer.from(data, "base64"));
  }
} finally {
  try { ws?.close(); } catch { /* not open */ }
  // Chrome keeps writing to its profile until it has actually exited; remove
  // the directory only after that, or rmSync races it and throws ENOTEMPTY.
  // Kill the whole process group: Chrome's GPU/renderer helpers survive a
  // SIGTERM to the main process alone and pile up across runs.
  const exited = new Promise((r) => chrome.once("exit", r));
  try { process.kill(-chrome.pid, "SIGTERM"); } catch { chrome.kill(); }
  await Promise.race([exited, sleep(3000)]);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
