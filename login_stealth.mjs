// Stealth 登录：用已装好的 Chrome for Testing 完成 Focusmate 登录，
// 去除 Playwright 自动化标记，尝试通过 Google 的 OAuth 检测。
//
// 登录态落在 ~/.focusmate-mcp/browser-data（与 MCP 共用），
// 成功后直接 `python3 main.py --start HH:MM` 下单。
//
// 用法：node login_stealth.mjs
// 5 分钟内在弹出的窗口里登录完（可试 Google 按钮，也可邮箱密码），
// 看到“登录成功”后关窗。

import { createRequire } from "module";
import fs from "fs";
import os from "os";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require("./temp/focusmate-mcp/node_modules/playwright");

const USER_DATA = path.join(os.homedir(), ".focusmate-mcp", "browser-data");
const CFT = path.join(
  os.homedir(),
  "Library/Caches/ms-playwright/chromium-1208/chrome-mac-arm64",
  "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
);
const LOGIN_URL = "https://www.focusmate.com/login";
const TIMEOUT_MS = 5 * 60 * 1000;

// 之前的 Vivaldi 探测污染过这个目录，重来一次
if (fs.existsSync(USER_DATA)) {
  fs.rmSync(USER_DATA, { recursive: true, force: true });
  console.log("已清空旧的 browser-data（之前 Vivaldi 探测留下的）");
}

const ctx = await chromium.launchPersistentContext(USER_DATA, {
  executablePath: CFT,
  headless: false,
  slowMo: 50,
  ignoreDefaultArgs: ["--enable-automation"],
  args: ["--disable-blink-features=AutomationControlled"],
});
await ctx.addInitScript(() => {
  Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  Object.defineProperty(navigator, "languages", { get: () => ["zh-CN", "zh", "en"] });
  Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3] });
});

const page = ctx.pages()[0] || (await ctx.newPage());
await page.goto(LOGIN_URL);
console.log("请在弹出的窗口里登录 Focusmate（Google 或邮箱密码都行），5 分钟有效…");

const start = Date.now();
let ok = false;
while (Date.now() - start < TIMEOUT_MS) {
  await page.waitForTimeout(1000);
  const url = page.url();
  if (url.includes("/dashboard") || url.includes("/home")) {
    try {
      await page.getByRole("button", { name: /book/i }).waitFor({ timeout: 5000 });
      ok = true;
      break;
    } catch {
      // 还没真正进 dashboard，继续等
    }
  }
}
console.log(ok ? "登录成功，登录态已保存，可以关窗去下单了。" : "登录超时，请重跑一次。");
await ctx.close();
process.exit(ok ? 0 : 1);
