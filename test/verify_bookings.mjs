// 只读：dump GET /v1/meetings/bookings 的响应结构，找 sessionTime / externalId /
// preferences / activityType，以及 cancel 需要的 source 参数。
import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true });
const page = ctx.pages()[0] || (await ctx.newPage());
await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(9000);

const out = await page.evaluate(async () => {
  const db = await new Promise((res, rej) => { const r = indexedDB.open("firebaseLocalStorageDb"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const all = await new Promise((res, rej) => { const tx = db.transaction("firebaseLocalStorage", "readonly"); const q = tx.objectStore("firebaseLocalStorage").getAll(); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
  const tok = all.map((r) => r?.value?.stsTokenManager?.accessToken).filter(Boolean)[0];
  const res = await fetch("https://api.focusmate.com/v1/meetings/bookings", { headers: { Authorization: tok } });
  const body = await res.json();
  return { status: res.status, body };
});
console.log("status:", out.status);
const b = out.body;
console.log("top keys:", Object.keys(b));
if (b.meetings) {
  console.log("meeting count:", b.meetings.length);
  console.log("first meeting:", JSON.stringify(b.meetings[0], null, 1).slice(0, 2500));
  console.log("all meetings (time/type/title/prefs):");
  for (const m of b.meetings) {
    console.log("  ", JSON.stringify({ sessionTime: m.sessionTime, meetingType: m.meetingType, title: m.title, externalId: m.externalId, activityType: m.activityType, preferences: m.preferences }));
  }
  console.log("participants sample:", JSON.stringify((b.participants || []).slice(0, 2), null, 1).slice(0, 900));
} else {
  console.log(JSON.stringify(b, null, 1).slice(0, 2500));
}
await ctx.close();
process.exit(0);
