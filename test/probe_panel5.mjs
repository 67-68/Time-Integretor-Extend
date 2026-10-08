// 只读：打开 Session Info 面板 → dump title/task 控件（不提交）。
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

const panelText = () => page.evaluate(() => {
  // 找包含 "Add task" 或 "Session title" 的最近面板容器
  const el = [...document.querySelectorAll("*")].find((e) => /Add a session title|Add task to this session|Session title/i.test(e.textContent || "") && e.childElementCount > 0 && (e.textContent || "").length < 1200);
  if (!el) return null;
  // 向上找 panel 根
  let p = el;
  for (let i = 0; i < 6 && p.parentElement; i++) p = p.parentElement;
  return { innerText: (p.innerText || "").slice(0, 1500), html: p.outerHTML.slice(0, 6000) };
});

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

const p1 = await panelText();
console.log("=== PANEL TEXT ===\n" + (p1?.innerText || "NONE"));
console.log("=== PANEL HTML (4000) ===\n" + (p1?.html || "NONE").slice(0, 4000));

// 点击 "Add task to this session"（或 "Add task"）打开 task 表单
const addTask = page.getByText(/Add task/i).first();
if (await addTask.count()) {
  await addTask.click({ timeout: 5000 }).catch((e) => console.log("addTask click err:", e.message.slice(0, 80)));
  await page.waitForTimeout(3500);
  const p2 = await panelText();
  console.log("\n=== AFTER CLICK 'Add task' ===\n" + (p2?.innerText || "NONE"));
  const form = await page.evaluate(() => ({
    inputs: [...document.querySelectorAll("input,textarea")].map((e) => ({ tag: e.tagName, ph: e.placeholder, id: e.id, maxlength: e.maxLength })),
    buttons: [...document.querySelectorAll("button")].map((b) => (b.textContent || "").trim().slice(0, 40)).filter(Boolean).filter((t) => /create|task|save|cancel/i.test(t)),
  }));
  console.log("FORM:", JSON.stringify(form, null, 1));
}
await page.screenshot({ path: "/tmp/probe-taskform.png", fullPage: true }).catch(() => {});
await ctx.close();
process.exit(0);
