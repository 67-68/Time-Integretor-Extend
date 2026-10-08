// 只读：打开一个 session 详情，找 "Add a session title" / "Add task" / quiet。
const MAX_MS = Number(process.env.PROBE_MAX_MS || 180000);
setTimeout(() => { console.log("WATCHDOG exit"); process.exit(0); }, MAX_MS).unref?.();

import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true });
const page = ctx.pages()[0] || (await ctx.newPage());
page.on("pageerror", (e) => console.log("PAGEERR:", String(e).slice(0, 120)));

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(10000);

async function scan(tag) {
  const hits = await page.evaluate(() => {
    const out = { texts: [], inputs: [], buttons: [] };
    for (const el of document.querySelectorAll("*")) {
      const t = (el.childElementCount === 0 ? el.textContent : "").trim();
      if (t && /add a session title|session title|add task|create task|task title|quiet/i.test(t) && t.length < 90) out.texts.push(t);
    }
    out.texts = [...new Set(out.texts)].slice(0, 25);
    out.inputs = [...document.querySelectorAll("input,textarea")].map((e) => ({ tag: e.tagName, type: e.type, id: e.id, ph: e.placeholder }));
    out.buttons = [...document.querySelectorAll("button")].map((b) => (b.textContent || "").trim().replace(/\s+/g, " ").slice(0, 45)).filter(Boolean).slice(0, 40);
    return out;
  });
  console.log(`\n===== ${tag} :: ${page.url()} =====`);
  console.log("texts:", JSON.stringify(hits.texts, null, 0));
  console.log("inputs:", JSON.stringify(hits.inputs));
  console.log("buttons:", JSON.stringify(hits.buttons));
}

// 1) 直接打开 session 详情页
const href = await page.evaluate(() => {
  const a = [...document.querySelectorAll("a[href*='/session/']")].pop();
  return a ? a.getAttribute("href") : null;
});
console.log("session href:", href);
if (href) {
  await page.goto("https://app.focusmate.com" + href, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(9000);
  await scan("session-detail");
  await page.screenshot({ path: "/tmp/probe-session.png", fullPage: true }).catch(() => {});
  console.log("=== BODY (2500) ===");
  console.log((await page.locator("body").innerText().catch(() => "")).slice(0, 2500));
}
await ctx.close();
process.exit(0);
