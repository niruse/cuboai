// Capture every docs image: serves the repo, opens harness.html?shot=<name> in
// headless Chrome, clips to #shot at 2x and writes docs/images/<name>.png.
// Node 18+ (global fetch/WebSocket: Node 22+), no npm packages.
//
//   node tools/docs-images/capture.mjs            # all shots
//   node tools/docs-images/capture.mjs card-live  # just these

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const REPO = resolve(HERE, "..", "..");
const OUT = join(REPO, "docs", "images");

export const SHOTS = [
  "card-live",
  "card-playback",
  "card-music",
  "card-editor",
  "timeline-card",
  "setup-login",
  "setup-cameras",
  "setup-options",
  "configure-general",
  "configure-streaming",
  "configure-protect",
  "entities-sensors",
  "entities-controls",
  "stream-sensor",
  "notification-restart",
];

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png" };

function serve() {
  const server = createServer((req, res) => {
    const path = normalize(join(REPO, decodeURIComponent(new URL(req.url, "http://x").pathname)));
    if (!path.startsWith(REPO) || !existsSync(path)) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { "content-type": TYPES[extname(path)] || "application/octet-stream", "cache-control": "no-store" });
    res.end(readFileSync(path));
  });
  return new Promise((ok) => server.listen(0, "127.0.0.1", () => ok(server)));
}

function findChrome() {
  const candidates = [
    process.env.CHROME,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error("No Chrome/Edge found; set CHROME=/path/to/chrome");
  return found;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((ok, fail) => {
    ws.onopen = ok;
    ws.onerror = fail;
  });
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && waiting.has(msg.id)) {
      const { ok, fail } = waiting.get(msg.id);
      waiting.delete(msg.id);
      msg.error ? fail(new Error(msg.error.message)) : ok(msg.result);
    }
  };
  const send = (method, params = {}) =>
    new Promise((ok, fail) => {
      const n = ++id;
      waiting.set(n, { ok, fail });
      ws.send(JSON.stringify({ id: n, method, params }));
    });
  return { send, close: () => ws.close() };
}

async function main() {
  const only = process.argv.slice(2);
  const shots = only.length ? SHOTS.filter((s) => only.includes(s)) : SHOTS;
  mkdirSync(OUT, { recursive: true });
  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}/tools/docs-images/harness.html`;
  const port = 9300 + Math.floor(Math.random() * 500);
  const profile = mkdtempSync(join(tmpdir(), "cuboai-docs-"));
  const chrome = spawn(findChrome(), [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--hide-scrollbars",
    "--lang=en-US",
    "--force-color-profile=srgb",
    "about:blank",
  ], { stdio: "ignore" });
  try {
    let version;
    for (let i = 0; i < 50 && !version; i++) {
      try {
        version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      } catch {
        await sleep(200);
      }
    }
    if (!version) throw new Error("Chrome did not start");
    for (const shot of shots) {
      const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
      const page = await cdp(target.webSocketDebuggerUrl);
      await page.send("Page.enable");
      await page.send("Runtime.enable");
      await page.send("Emulation.setDeviceMetricsOverride", { width: 900, height: 1600, deviceScaleFactor: 2, mobile: false });
      await page.send("Page.navigate", { url: `${base}?shot=${shot}` });
      let ready = false;
      for (let i = 0; i < 100 && !ready; i++) {
        await sleep(150);
        const r = await page.send("Runtime.evaluate", { expression: "document.body && document.body.dataset.ready === '1'", returnByValue: true });
        ready = r.result.value === true;
      }
      if (!ready) throw new Error(`${shot}: page never became ready`);
      await sleep(400); // let fonts and the last paint land
      const box = await page.send("Runtime.evaluate", {
        // The first child with a box: a scene may start with a <style>.
        expression: "(() => { const s = document.getElementById('shot'); const e = [...s.children].find((c) => c.getBoundingClientRect().width > 0) || s; const r = e.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height, err: !!document.querySelector('#shot > pre')}; })()",
        returnByValue: true,
      });
      const clip = box.result.value;
      if (clip.err) throw new Error(`${shot}: the scene threw (open harness.html?shot=${shot} to see why)`);
      const pad = 12;
      const shotPng = await page.send("Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: true,
        clip: { x: Math.max(0, clip.x - pad), y: Math.max(0, clip.y - pad), width: clip.width + 2 * pad, height: clip.height + 2 * pad, scale: 1 },
      });
      writeFileSync(join(OUT, `${shot}.png`), Buffer.from(shotPng.data, "base64"));
      console.log(`${shot}.png  ${Math.round(clip.width)}x${Math.round(clip.height)}`);
      page.close();
      await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`).catch(() => {});
    }
  } finally {
    chrome.kill();
    server.close();
  }
}

main().catch((e) => {
  console.error(e.message || e);
  process.exit(1);
});
