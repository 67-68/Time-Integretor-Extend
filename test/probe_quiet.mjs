// 只读：点 Quiet Mode 开关，确认状态翻转（仅在预约面板里，不提交下单）。
const MAX_MS = Number(process.env.PROBE_MAX_MS || 150000);
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

const state = () => page.evaluate(() => {
  const label = document.querySelector("#quietModeLabel");
  const wrap = label?.closest("div")?.parentElement;
  const inp = wrap?.querySelector("input.p-toggleswitch-input");
  return { checked: inp?.checked ?? null, labelText: (label?.textContent || "").trim() };
});
console.log("before:", JSON.stringify(await state()));

// 用 Playwright 点击 toggleswitch
const ts = page.locator("p-toggleswitch").first();
console.log("toggleswitch count:", await page.locator("p-toggleswitch").count());
if (await ts.count()) {
  await ts.click({ timeout: 5000 }).catch((e) => console.log("click err:", e.message.slice(0, 80)));
  await page.waitForTimeout(1500);
  console.log("after click 1:", JSON.stringify(await state()));
  await ts.click({ timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(1000);
  console.log("after click 2 (revert):", JSON.stringify(await state()));
}
await ctx.close();
process.exit(0);
