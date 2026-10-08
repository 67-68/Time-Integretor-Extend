// 只读：正确点击 tile 主体 → 右侧 dynamic panel 出现 Session Info。
const MAX_MS = Number(process.env.PROBE_MAX_MS || 180000);
setTimeout(() => { console.log("WATCHDOG exit"); process.exit(0); }, MAX_MS).unref?.();

import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true, viewport: { width: 1600, height: 900 } });
const page = ctx.pages()[0] || (await ctx.newPage());

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(10000);

const box = await page.evaluate(() => {
  const tile = document.querySelector("fm-booked-session-tile");
  if (!tile) return null;
  const div = tile.querySelector("div[class*='rounded-tr-lg']") || tile.firstElementChild;
  const r = div.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height };
});
console.log("tile rect:", JSON.stringify(box));

const before = await page.locator("body").innerText().catch(() => "");
console.log("has 'Add a session title' before:", /Add a session title/i.test(before), "| 'Add task' before:", /Add task/i.test(before));

// 点击 tile 右侧中部（避开 avatar / 按钮）
await page.mouse.click(box.x + box.w - 30, box.y + Math.round(box.h * 0.35));
await page.waitForTimeout(5000);
await page.screenshot({ path: "/tmp/probe-sessioninfo.png", fullPage: true }).catch(() => {});

const after = await page.locator("body").innerText().catch(() => "");
console.log("has 'Add a session title' after:", /Add a session title/i.test(after), "| 'Add task' after:", /Add task/i.test(after));

const scan = await page.evaluate(() => {
  const texts = [];
  for (const el of document.querySelectorAll("*")) {
    const t = (el.childElementCount === 0 ? el.textContent : "").trim();
    if (t && /add a session title|session title|add task|create task|task title|quiet|my task/i.test(t) && t.length < 95) texts.push(t);
  }
  return { url: location.href, texts: [...new Set(texts)].slice(0, 30), inputs: [...document.querySelectorAll("input,textarea")].map((e) => ({ tag: e.tagName, type: e.type, ph: e.placeholder, id: e.id })) };
});
console.log("SCAN:", JSON.stringify(scan, null, 1));
console.log("=== BODY tail (2000) ===");
console.log(after.slice(-2000));
await ctx.close();
process.exit(0);
