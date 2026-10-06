// 诊断：复现 booking 的“点格子”步骤，然后 dump 点击位置附近的 DOM，
// 确认 Focusmate 实际渲染的 Book 按钮是什么元素/可访问名。
// 用法：node inspect_booking.mjs [HH:MM]，默认 18:00
import { createRequire } from "module";
import os from "os";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const USER_DATA = path.join(os.homedir(), ".focusmate-mcp", "browser-data");
const arg = process.argv[2] || "18:00";
const [hh, mm] = arg.split(":").map(Number);

const ctx = await chromium.launchPersistentContext(USER_DATA, {
  headless: true,
  args: ["--disable-blink-features=AutomationControlled"],
});
const page = ctx.pages()[0] || (await ctx.newPage());

await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(3000);
console.log("url:", page.url());

// 选 50 min
try {
  const b50 = page.getByRole("button", { name: "50 min", exact: true });
  await b50.waitFor({ timeout: 10000 });
  await b50.click();
  console.log("clicked 50 min");
} catch (e) {
  console.log("50min button not found:", e.message.split("\n")[0]);
}
await page.waitForTimeout(2000);

// 定位 6pm
const hour12 = hh > 12 ? hh - 12 : (hh === 0 ? 12 : hh);
const ampm = hh >= 12 ? "pm" : "am";
const hourLabel = `${hour12}${ampm}`;
const timeLabel = page.locator(`text="${hourLabel}"`).first();
await timeLabel.waitFor({ timeout: 5000 });
await timeLabel.scrollIntoViewIfNeeded();
await page.waitForTimeout(500);
const labelBox = await timeLabel.boundingBox();
console.log("time label box:", labelBox);

// pixels per hour
let pixelsPerHour = 192;
for (const off of [1, -1]) {
  const rh = (hh + off + 24) % 24;
  const rh12 = rh > 12 ? rh - 12 : (rh === 0 ? 12 : rh);
  const ramp = rh >= 12 ? "pm" : "am";
  try {
    const rb = await page.locator(`text="${rh12}${ramp}"`).first().boundingBox();
    if (rb) {
      pixelsPerHour = Math.abs(rb.y - labelBox.y);
      console.log("pixelsPerHour:", pixelsPerHour);
      break;
    }
  } catch {}
}

// 列头：今天日期
const now = new Date();
const dayName = now.toLocaleDateString("en-US", { weekday: "short" });
const dayOfMonth = now.getDate();
const headerText = `${dayName} ${dayOfMonth}`;
console.log("header:", headerText);
const hb = await page.locator(`text="${headerText}"`).first().boundingBox().catch(() => null);
console.log("header box:", hb);
const clickX = hb ? hb.x + hb.width / 2 : labelBox.x + 100;
const clickY = labelBox.y + (mm / 60) * pixelsPerHour + pixelsPerHour / 8;
console.log("click at:", clickX, clickY);

await page.mouse.click(clickX, clickY);
await page.waitForTimeout(1500);

// dump 所有含 Book 的元素
const found = await page.evaluate(() => {
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = walker.nextNode())) {
    const el = n;
    const text = (el.textContent || "").trim();
    if (/book/i.test(text) && text.length < 80) {
      out.push({
        tag: el.tagName,
        role: el.getAttribute("role"),
        cls: (el.className || "").toString().slice(0, 80),
        text: text.slice(0, 80),
        aria: el.getAttribute("aria-label"),
        rect: (() => {
          const r = el.getBoundingClientRect();
          return r.width > 0 ? `${Math.round(r.x)},${Math.round(r.y)},${Math.round(r.width)}x${Math.round(r.height)}` : null;
        })(),
      });
    }
  }
  return out.slice(0, 30);
});
console.log("Book-related elements:");
for (const f of found) console.log(JSON.stringify(f, null, 2));

// 点击位置处最内层元素
const atPoint = await page.evaluate((x, y) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  return {
    tag: el.tagName,
    role: el.getAttribute("role"),
    cls: (el.className || "").toString().slice(0, 120),
    text: (el.textContent || "").trim().slice(0, 80),
    aria: el.getAttribute("aria-label"),
    html: el.outerHTML.slice(0, 500),
  };
}, clickX, clickY);
console.log("element at click point:");
console.log(JSON.stringify(atPoint, null, 2));

await ctx.close();
