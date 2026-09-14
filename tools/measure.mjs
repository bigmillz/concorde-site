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

const profile = mkdtempSync(join(tmpdir(), "measure-"));
const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
  `--user-data-dir=${profile}`, "--remote-debugging-port=0", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const portFile = join(profile, "DevToolsActivePort");
for (let i = 0; i < 100 && !existsSync(portFile); i++) await sleep(50);
const port = readFileSync(portFile, "utf8").split("\n")[0];
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = targets.find((t) => t.type === "page");

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
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
  pending.set(n, (msg) => (msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result)));
  ws.send(JSON.stringify({ id: n, method, params }));
});

try {
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 768 });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  await send("Page.navigate", { url });
  for (let i = 0; i < 200 && !events.includes("Page.loadEventFired"); i++) await sleep(50);
  await send("Runtime.evaluate", { expression: "document.fonts.ready", awaitPromise: true });
  await sleep(300);                                     // one more layout pass after fonts
  const { result, exceptionDetails } = await send("Runtime.evaluate", {
    expression: `(async () => (${expr}))()`, awaitPromise: true, returnByValue: true,
  });
  if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || "evaluate failed");
  console.log(JSON.stringify(result.value, null, 2));
  if (shot) {
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(shot, Buffer.from(data, "base64"));
  }
} finally {
  ws.close();
  // Chrome keeps writing to its profile until it has actually exited; remove
  // the directory only after that, or rmSync races it and throws ENOTEMPTY.
  const exited = new Promise((r) => chrome.once("exit", r));
  chrome.kill();
  await Promise.race([exited, sleep(3000)]);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
