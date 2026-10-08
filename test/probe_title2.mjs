// 只读：打开 Session Info 面板，定位 session title 铅笔按钮与输入框。
const MAX_MS = Number(process.env.PROBE_MAX_MS || 200000);
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
  const t = document.querySelector("fm-booked-session-tile");
  const d = t?.querySelector("div[class*='rounded-tr-lg']");
  const r = d?.getBoundingClientRect();
  return r ? { x: r.x, y: r.y, w: r.width, h: r.height } : null;
});
await page.mouse.click(box.x + box.w - 30, box.y + Math.round(box.h * 0.35));
await page.waitForTimeout(5000);

// 找面板容器：含 "Add task" 的有界元素
const panel = await page.evaluate(() => {
  const anchor = [...document.querySelectorAll("*")].find((e) => e.childElementCount === 0 && /Add task/i.test(e.textContent || ""));
  if (!anchor) return { err: "no Add task" };
  let p = anchor;
  for (let i = 0; i < 8 && p.parentElement; i++) p = p.parentElement;
  const pencils = [...p.querySelectorAll("*")].filter((e) => /pencil/i.test(e.className || "")).map((e) => {
    const r = e.getBoundingClientRect();
    return { tag: e.tagName, cls: e.className, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) };
  });
  const titleLike = [...p.querySelectorAll("*")].filter((e) => e.childElementCount === 0 && (e.textContent || "").trim()).map((e) => (e.textContent || "").trim().slice(0, 40)).slice(0, 40);
  return { panelText: (p.innerText || "").slice(0, 1200), pencils, titleLike };
});
console.log("=== PANEL ===");
console.log("TEXT:", panel.panelText || panel.err);
console.log("PENCILS:", JSON.stringify(panel.pencils));
console.log("LEAF TEXTS:", JSON.stringify(panel.titleLike));

// 点铅笔（用坐标）
if (panel.pencils && panel.pencils.length) {
  const pc = panel.pencils.find((x) => x.w > 0);
  if (pc) {
    await page.mouse.click(pc.x + pc.w / 2, pc.y + 8);
    await page.waitForTimeout(2500);
    const ins = await page.evaluate(() => [...document.querySelectorAll("input,textarea")].map((e) => ({ tag: e.tagName, ph: e.placeholder, maxlength: e.maxLength, value: e.value, visible: !!e.offsetParent })).filter((e) => e.visible));
    console.log("=== INPUTS AFTER PENCIL ===");
    console.log(JSON.stringify(ins, null, 1));
    await page.screenshot({ path: "/tmp/probe-title2.png", fullPage: true }).catch(() => {});
  }
}
await ctx.close();
process.exit(0);
