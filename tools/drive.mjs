// Drive the sandbox page (tools/sandbox.py) in headless Chromium over the DevTools protocol; needs only node and chromium.
//   node tools/drive.mjs PORT steps.json      (SIZE=1280x800 for another window, SCALE=2 for sharper screenshots; OUT=dir for them, default: beside the steps)
// steps.json is a list of: ["type","gal x3"] ["key","Enter"] ["key","ArrowUp",8] (8: Shift) ["click","css selector",2] (2: twice)
//   ["assert","js"] ["until","js",timeoutMs] ["reload"] (assertions/JS errors fail the run)
//   ["wait",ms] ["shot","name"] (name.png) ["shot","name",[x,y,w,h]] (that part only) ["dark",true] ["eval","js"] (printed)
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
const [port, file] = process.argv.slice(2), dir = process.env.OUT || path.dirname(path.resolve(file));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "mf-drive-"));
const [w, h] = (process.env.SIZE || "1720x1000").split("x").map(Number);
const chrome = spawn("chromium", ["--headless=new", "--no-sandbox", "--disable-gpu", `--user-data-dir=${profile}`, "--remote-debugging-port=0", `--window-size=${w},${h}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target, ws;
try {
for (let i = 0; i < 50 && !target; i++) { await sleep(200); try { target = (await (await fetch(`http://127.0.0.1:${fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]}/json`)).json()).find((t) => t.type === "page"); } catch {} }
if (!target) throw new Error("Chromium did not start");
ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0; const waiting = new Map();
ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && waiting.has(d.id)) { waiting.get(d.id)(d.result || d); waiting.delete(d.id); } };
const send = (method, params = {}) => new Promise((r) => { waiting.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
const ev = async (expression) => {
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  if (result.error) throw new Error(result.error.message);
  return result.result?.value;
};
async function until(expression, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (!await ev(expression)) {
    if (Date.now() >= deadline) throw new Error(`Timed out: ${expression}`);
    await sleep(100);
  }
}
await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: +(process.env.SCALE || 1), mobile: false });
await send("Page.navigate", { url: `http://127.0.0.1:${port}/` });
await sleep(1500);
const KEYS = { Enter: 13, Tab: 9, Escape: 27, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39, Backspace: 8, Delete: 46 };
async function key(k, mods = 0) {
  const base = { key: k, code: k, windowsVirtualKeyCode: KEYS[k], nativeVirtualKeyCode: KEYS[k], modifiers: mods };
  await send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base, ...(k === "Enter" ? { text: "\r" } : {}) });
  await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}
async function type(text) { for (const ch of text) { await send("Input.dispatchKeyEvent", { type: "keyDown", key: ch, text: ch }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: ch }); await sleep(15); } }
async function click(selector, n = 1) {
  const box = await ev(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return null; const b = e.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; })()`);
  if (!box) throw new Error(`No element: ${selector}`);
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box[0] - 3, y: box[1] - 2 });
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box[0], y: box[1] });
  for (let i = 1; i <= n; i++) { await send("Input.dispatchMouseEvent", { type: "mousePressed", x: box[0], y: box[1], button: "left", clickCount: i }); await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box[0], y: box[1], button: "left", clickCount: i }); await sleep(60); }
}
for (const [op, a, b] of JSON.parse(fs.readFileSync(file, "utf8"))) {
  if (op === "type") await type(a);
  else if (op === "key") await key(a, b || 0);
  else if (op === "click") await click(a, b || 1);
  else if (op === "assert") { if (!await ev(a)) throw new Error(`Assertion failed: ${a}`); }
  else if (op === "until") await until(a, b);
  else if (op === "reload") { await send("Page.reload"); await sleep(500); }
  else if (op === "wait") await sleep(a);
  else if (op === "eval") console.log(a, "=>", JSON.stringify(await ev(a)));
  else if (op === "dark") await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: a ? "dark" : "light" }] });
  else if (op === "shot") fs.writeFileSync(`${dir}/${a}.png`, Buffer.from((await send("Page.captureScreenshot", { format: "png", ...(b ? { clip: { x: b[0], y: b[1], width: b[2], height: b[3], scale: 1 } } : {}) })).data, "base64"));
  await sleep(80);
}
await sleep(2300); // let the page flush its trace
} finally {
ws?.close();
if (chrome.exitCode === null) await new Promise((done) => { chrome.on("exit", done); chrome.kill(); });
try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5 }); } catch { /* Chromium was still writing: the system clears its temporary folder */ }
}
