// 只读 dump dashboard：会话卡片、按钮、输入框、quiet/task/title/cancel 相关元素。
// 绝不点击会下单/取消的按钮。
const MAX_MS = Number(process.env.PROBE_MAX_MS || 90000);
setTimeout(() => { console.log("WATCHDOG exit"); process.exit(0); }, MAX_MS).unref?.();

import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true });
const page = ctx.pages()[0] || (await ctx.newPage());

async function dump(tag) {
  const info = await page.evaluate(() => {
    const btns = [...document.querySelectorAll("button,a[role=button]")].map((b) => ({
      text: (b.textContent || "").trim().replace(/\s+/g, " ").slice(0, 50),
      aria: b.getAttribute("aria-label"),
      icon: b.querySelector("i")?.className || null,
      cls: (b.className || "").toString().slice(0, 60),
    })).filter((b) => b.text || b.aria || b.icon);
    const inputs = [...document.querySelectorAll("input,textarea")].map((e) => ({
      tag: e.tagName, type: e.type, id: e.id, name: e.name,
      placeholder: e.placeholder, aria: e.getAttribute("aria-label"),
    }));
    const custom = [...document.querySelectorAll("[id*=quiet],[class*=quiet],[id*=task],[class*=task],[id*=title],[class*=title]")]
      .slice(0, 30).map((e) => ({ tag: e.tagName, id: e.id, cls: (e.className || "").toString().slice(0, 70), text: (e.textContent || "").trim().slice(0, 60) }));
    return { url: location.href, btns, inputs, custom };
  });
  console.log(`\n===== ${tag} :: ${info.url} =====`);
  console.log("BUTTONS(" + info.btns.length + "):");
  for (const b of info.btns) console.log("   ", JSON.stringify(b));
  console.log("INPUTS(" + info.inputs.length + "):");
  for (const i of info.inputs) console.log("   ", JSON.stringify(i));
  console.log("QUIET/TASK/TITLE nodes(" + info.custom.length + "):");
  for (const c of info.custom) console.log("   ", JSON.stringify(c));
}

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(9000);
await page.screenshot({ path: "/tmp/probe-dashboard.png", fullPage: true }).catch(() => {});
await dump("dashboard");
console.log("\n=== BODY TEXT ===");
console.log((await page.locator("body").innerText().catch(() => "")).slice(0, 3000));

await ctx.close();
process.exit(0);
