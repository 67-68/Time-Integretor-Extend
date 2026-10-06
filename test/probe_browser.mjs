// 探测哪种方式能拉起 Vivaldi 且不崩溃。用法：node probe_browser.mjs
// 每个 case 限时 20 秒，打印 OK / FAIL + 原因。
import { createRequire } from "module";
import os from "os";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const VIVALDI = "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi";
const PROBE_DIR = path.join(os.homedir(), ".focusmate-mcp", "probe-data");

async function attempt(name, fn) {
  try {
    const r = await fn();
    console.log(`[${name}] OK (${r})`);
    return true;
  } catch (e) {
    console.log(`[${name}] FAIL: ${(e.message || String(e)).split("\n")[0]}`);
    return false;
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// case1: 纯启动（非 persistent），看二进制本身能不能被 playwright 驱动
await attempt("plain-launch", async () => {
  const b = await chromium.launch({ executablePath: VIVALDI, headless: false });
  const p = await b.newPage();
  await p.goto("about:blank");
  await sleep(3000);
  const v = await p.evaluate(() => navigator.userAgent);
  await b.close();
  return v.slice(0, 60);
});

// case2: persistent + 去掉 --enable-automation（Vivaldi 疑似被这个搞崩）
await attempt("persistent-no-automation-flag", async () => {
  const ctx = await chromium.launchPersistentContext(PROBE_DIR + "-2", {
    executablePath: VIVALDI,
    headless: false,
    ignoreDefaultArgs: ["--enable-automation"],
    args: ["--disable-blink-features=AutomationControlled"],
  });
  const p = ctx.pages()[0] || (await ctx.newPage());
  await p.goto("about:blank");
  await sleep(3000);
  await ctx.close();
  return "alive 3s";
});

// case3: persistent + 默认参数（当前 login 脚本的方式，对照组）
await attempt("persistent-default", async () => {
  const ctx = await chromium.launchPersistentContext(PROBE_DIR + "-3", {
    executablePath: VIVALDI,
    headless: false,
  });
  const p = ctx.pages()[0] || (await ctx.newPage());
  await p.goto("about:blank");
  await sleep(3000);
  await ctx.close();
  return "alive 3s";
});

console.log("done");
