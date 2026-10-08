// 只读：session tile 结构 + 点击 session 打开右侧面板找 title/task/quiet。
const MAX_MS = Number(process.env.PROBE_MAX_MS || 150000);
setTimeout(() => { console.log("WATCHDOG exit"); process.exit(0); }, MAX_MS).unref?.();

import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true });
const page = ctx.pages()[0] || (await ctx.newPage());

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(10000);

// A) Cancel 按钮细节
const cancel = page.getByRole("button", { name: /^Cancel session:/ }).first();
console.log("cancel count:", await page.getByRole("button", { name: /^Cancel session:/ }).count());
console.log("cancel box:", JSON.stringify(await cancel.boundingBox().catch(() => null)));
const cancelInfo = await cancel.evaluate((el) => ({
  cls: el.className,
  title: el.getAttribute("title"),
  aria: el.getAttribute("aria-label"),
  html: el.outerHTML.slice(0, 700),
})).catch(() => null);
console.log("CANCEL BTN:", JSON.stringify(cancelInfo, null, 1));

// B) 找 session tile 里可点击的元素（链接/标题）
const links = await page.evaluate(() =>
  [...document.querySelectorAll("a[href]")].map((a) => a.getAttribute("href")).filter((h) => h.includes("/session")).slice(0, 10));
console.log("session links:", JSON.stringify(links));

// C) 尝试点第一个 tile 的标题文字（不是按钮）打开右侧面板
const tileText = page.locator('text=/minute session/').first();
console.log("tileText count:", await page.locator('text=/minute session/').count());

// 用 session 卡片容器点击：找包含 "Cancel session" 按钮的 tile，点它的非按钮区域
const clicked = await page.evaluate(() => {
  const btn = [...document.querySelectorAll("button")].find((b) => /^Cancel session:/.test((b.textContent || "").trim()));
  if (!btn) return "no btn";
  // tile = 向上找到包含 Join/时间 的最小容器
  let tile = btn;
  for (let i = 0; i < 6 && tile.parentElement; i++) {
    tile = tile.parentElement;
    if ((tile.textContent || "").includes("minute session")) break;
  }
  (window).__tile = tile;
  return { tileTag: tile.tagName, tileCls: (tile.className || "").toString().slice(0, 80), html: tile.outerHTML.slice(0, 2500) };
});
console.log("TILE:", JSON.stringify(clicked, null, 1));

await page.screenshot({ path: "/tmp/probe-tile.png", fullPage: true }).catch(() => {});
await ctx.close();
process.exit(0);
