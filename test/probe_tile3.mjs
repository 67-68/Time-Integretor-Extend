// 只读：精确定位 session tile 的可点击容器并 dump 结构。
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

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(10000);

const info = await page.evaluate(() => {
  // 找含 "Cancel session" 的 button，向上找 6 层，dump 每一层的 tag/class/是否有 click
  const btn = [...document.querySelectorAll("button")].find((b) => /^Cancel session:/.test((b.textContent || "").trim()));
  if (!btn) return { err: "no cancel btn" };
  const chain = [];
  let el = btn;
  for (let i = 0; i < 7 && el; i++) {
    chain.push({
      i, tag: el.tagName,
      cls: (el.className || "").toString().slice(0, 100),
      text: (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 55),
      childCount: el.childElementCount,
    });
    el = el.parentElement;
  }
  // tile 内所有元素里，class 含 cursor 的
  const tile = btn.parentElement?.parentElement?.parentElement?.parentElement?.parentElement || btn;
  const cursors = [...tile.querySelectorAll("*")].filter((e) => /cursor|click/i.test(e.className || "")).map((e) => ({
    tag: e.tagName, cls: (e.className || "").toString().slice(0, 80), text: (e.textContent || "").trim().slice(0, 50),
  })).slice(0, 15);
  const titleEls = [...tile.querySelectorAll("*")].filter((e) => e.childElementCount === 0 && /test/i.test(e.textContent || "")).map((e) => ({
    tag: e.tagName, cls: (e.className || "").toString().slice(0, 80), text: (e.textContent || "").trim().slice(0, 50),
  })).slice(0, 8);
  return { chain, cursors, titleEls };
});
console.log(JSON.stringify(info, null, 1));
await ctx.close();
process.exit(0);
