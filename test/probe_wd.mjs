// 硬看门狗：无论发生什么，最多运行 N 秒后强制退出。
const MAX_MS = Number(process.env.PROBE_MAX_MS || 45000);
const t = setTimeout(() => {
  console.log("WATCHDOG: force exit after " + MAX_MS + "ms");
  process.exit(0);
}, MAX_MS);
t.unref?.();

import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const route = process.argv[2] || "/dashboard";
let ctx;
try {
  ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true });
  const page = ctx.pages()[0] || (await ctx.newPage());
  page.on("pageerror", (e) => console.log("PAGEERR:", String(e).slice(0, 150)));
  await page.goto("https://app.focusmate.com" + route, { waitUntil: "domcontentloaded" });
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(2500);
    const len = await page.evaluate(() => (document.body ? document.body.innerText.length : -1));
    console.log(`t=${(i + 1) * 2.5}s url=${page.url()} bodyLen=${len}`);
    if (len > 50) break;
  }
  const htmlLen = await page.evaluate(() => document.querySelector("app-root")?.innerHTML.length ?? -1);
  console.log("app-root html len:", htmlLen);
  const stub = await page.evaluate(() => (document.querySelector("app-root")?.innerHTML || "").slice(0, 1200));
  console.log("app-root head:", stub);
} catch (e) {
  console.log("ERR:", e?.message?.slice(0, 200) || String(e));
} finally {
  try { await ctx?.close(); } catch {}
  clearTimeout(t);
  process.exit(0);
}
