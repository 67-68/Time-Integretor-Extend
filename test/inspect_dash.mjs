// 诊断：dump dashboard 当前可见的按钮/文本/URL，搞清楚日历和 50min 按钮在哪。
import { createRequire } from "module";
import os from "os";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const USER_DATA = path.join(os.homedir(), ".focusmate-mcp", "browser-data");
const ctx = await chromium.launchPersistentContext(USER_DATA, {
  headless: true,
  args: ["--disable-blink-features=AutomationControlled"],
});
const page = ctx.pages()[0] || (await ctx.newPage());

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(8000);
console.log("url:", page.url());
console.log("viewport:", page.viewportSize());

const info = await page.evaluate(() => {
  const btns = [...document.querySelectorAll("button")]
    .map((b) => ({
      text: (b.textContent || "").trim().slice(0, 40),
      aria: b.getAttribute("aria-label"),
    }))
    .filter((b) => b.text || b.aria)
    .slice(0, 40);
  const bodyText = (document.body.innerText || "").slice(0, 1500);
  return { btns, bodyText };
});
console.log("BUTTONS:");
info.btns.forEach((b) => console.log("  ", JSON.stringify(b)));
console.log("BODY TEXT (first 1500):");
console.log(info.bodyText);

await ctx.close();
