// 诊断 v2：更长等待 + console/pageerror + 每 5s 采样 body 文本长度
import { createRequire } from "module";
import os from "os";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const USER_DATA = path.join(os.homedir(), ".focusmate-mcp", "browser-data");
const ctx = await chromium.launchPersistentContext(USER_DATA, {
  headless: true,
});
const page = ctx.pages()[0] || (await ctx.newPage());
page.on("console", (m) => {
  if (m.type() === "error") console.log("CONSOLE ERR:", m.text().slice(0, 200));
});
page.on("pageerror", (e) => console.log("PAGE ERROR:", String(e).slice(0, 200)));

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
for (let i = 1; i <= 5; i++) {
  await page.waitForTimeout(5000);
  const info = await page.evaluate(() => ({
    url: location.href,
    bodyLen: document.body ? document.body.innerText.length : -1,
    btnCount: document.querySelectorAll("button").length,
    title: document.title,
    readyState: document.readyState,
  }));
  console.log(`t=${i * 5}s`, JSON.stringify(info));
  if (info.bodyLen > 0) {
    const txt = await page.evaluate(() => document.body.innerText.slice(0, 800));
    console.log("TEXT:", txt);
    break;
  }
}
await ctx.close();
