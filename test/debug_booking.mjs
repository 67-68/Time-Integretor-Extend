// 深度调试 booking 流程：分步执行 MCP 的动作，每步用 Playwright 定位器
// （穿透 shadow DOM）dump 真实可访问名 + 坐标 + 截图。
//
// 用法：
//   node debug_booking.mjs              # headless，自动跑完
//   node debug_booking.mjs --headed     # 弹窗，能亲眼看到每步
//   node debug_booking.mjs --start 21:00
import { createRequire } from "module";
import os from "os";
import fs from "fs";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const USER_DATA = path.join(os.homedir(), ".focusmate-mcp", "browser-data");
const SHOT_DIR = path.join(process.cwd(), "temp", "debug-shots");
const headed = process.argv.includes("--headed");
fs.mkdirSync(SHOT_DIR, { recursive: true });

const si = process.argv.indexOf("--start");
const startStr = si >= 0 ? process.argv[si + 1] : "18:00";
const [hh, mm] = startStr.split(":").map(Number);

const ctx = await chromium.launchPersistentContext(USER_DATA, {
  headless: !headed,
  slowMo: headed ? 150 : 0,
});
const page = ctx.pages()[0] || (await ctx.newPage());

let step = 0;
async function shot(tag) {
  step += 1;
  const f = path.join(SHOT_DIR, `${String(step).padStart(2, "0")}-${tag}.png`);
  await page.screenshot({ path: f, fullPage: true }).catch(() => {});
  return f;
}

async function dump(tag) {
  const frameUrls = page.frames().map((f) => f.url());
  const out = { tag, url: page.url(), frames: frameUrls, buttons: [] };
  const btns = page.getByRole("button");
  const n = await btns.count().catch(() => 0);
  for (let i = 0; i < n; i++) {
    const b = btns.nth(i);
    const t = await b.innerText().catch(() => "");
    const aria = await b.getAttribute("aria-label").catch(() => null);
    const title = await b.getAttribute("title").catch(() => null);
    const vis = await b.isVisible().catch(() => false);
    const box = await b.boundingBox().catch(() => null);
    out.buttons.push({
      text: (t || "").trim().slice(0, 40),
      aria: aria ? aria.slice(0, 40) : null,
      title,
      visible: vis,
      box: box ? `${Math.round(box.x)},${Math.round(box.y)} ${Math.round(box.width)}x${Math.round(box.height)}` : null,
    });
  }
  const bodyLen = await page.locator("body").innerText().then((t) => t.length).catch(() => -1);
  console.log(`\n===== ${tag} =====`);
  console.log("url:", out.url, "| body innerText len:", bodyLen);
  console.log("frames:", frameUrls.length);
  for (const u of frameUrls) console.log("   frame:", u);
  const appRoot = await page.evaluate(() => {
    const ar = document.querySelector("app-root");
    return ar ? { htmlLen: ar.innerHTML.length, childCount: ar.children.length } : null;
  }).catch(() => null);
  console.log("app-root:", JSON.stringify(appRoot));
  const htmlLen = (await page.content().catch(() => "")).length;
  console.log("page.content len:", htmlLen);
  console.log("buttons(%d):", out.buttons.length);
  for (const b of out.buttons) console.log("   ", JSON.stringify(b));
  const f = await shot(tag);
  console.log("screenshot:", f);
}

console.log("headless:", !headed, "| target:", startStr);
await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);
await dump("1-dashboard");

// 选 50 min
try {
  const b50 = page.getByRole("button", { name: "50 min", exact: true });
  await b50.waitFor({ timeout: 10000 });
  await b50.click();
  await page.waitForTimeout(2500);
  await dump("2-after-50min");
} catch (e) {
  console.log("!! 50min 按钮失败:", e.message.split("\n")[0]);
  await dump("2-50min-FAILED");
}

// 定位 6pm 并点格子
const hour12 = hh > 12 ? hh - 12 : hh === 0 ? 12 : hh;
const ampm = hh >= 12 ? "pm" : "am";
const hourLabel = `${hour12}${ampm}`;
console.log("hourLabel:", hourLabel);

const timeLabel = page.locator(`text="${hourLabel}"`).first();
try {
  await timeLabel.waitFor({ timeout: 5000 });
  await timeLabel.scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);
} catch (e) {
  console.log("!! 找不到时间标签:", e.message.split("\n")[0]);
}
const labelBox = await timeLabel.boundingBox().catch(() => null);
console.log("time label box:", labelBox && JSON.stringify(labelBox));

let pixelsPerHour = 192;
for (const off of [1, -1]) {
  const rh = (hh + off + 24) % 24;
  const rh12 = rh > 12 ? rh - 12 : rh === 0 ? 12 : rh;
  const ramp = rh >= 12 ? "pm" : "am";
  const rb = await page.locator(`text="${rh12}${ramp}"`).first().boundingBox().catch(() => null);
  if (rb) {
    pixelsPerHour = Math.abs(rb.y - labelBox.y);
    console.log(`pixelsPerHour(from ${rh12}${ramp}):`, pixelsPerHour);
    break;
  }
}

const now = new Date();
const dayName = now.toLocaleDateString("en-US", { weekday: "short" });
const dayOfMonth = now.getDate();
const headerText = `${dayName} ${dayOfMonth}`;
const hb = await page.locator(`text="${headerText}"`).first().boundingBox().catch(() => null);
console.log("header:", headerText, hb && JSON.stringify(hb));
const clickX = hb ? hb.x + hb.width / 2 : (labelBox ? labelBox.x + 100 : 300);
const clickY = labelBox ? labelBox.y + (mm / 60) * pixelsPerHour + pixelsPerHour / 8 : 300;
console.log("click at:", Math.round(clickX), Math.round(clickY));

await page.mouse.click(clickX, clickY);
await page.waitForTimeout(2500);
await dump("3-after-slot-click");

// 点后：元素是什么？
const atPoint = await page.evaluate(
  ([x, y]) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const path = [];
    let n = el;
    for (let i = 0; i < 4 && n; i++) {
      path.push(`${n.tagName}${n.getAttribute("role") ? `[role=${n.getAttribute("role")}]` : ""}.${(n.className || "").toString().split(" ")[0]}`);
      n = n.parentElement;
    }
    return { text: (el.textContent || "").trim().slice(0, 60), path };
  },
  [clickX, clickY]
);
console.log("element at click point:", JSON.stringify(atPoint));

// 原地再点一次
console.log(">>> 原地补点");
await page.mouse.click(clickX, clickY);
await page.waitForTimeout(2500);
await dump("4-after-second-click");

console.log("\n调试完成，截图目录:", SHOT_DIR);
await ctx.close();
