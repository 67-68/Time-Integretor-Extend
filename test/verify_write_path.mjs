// 风险清零测试：
//  (1) PUT /v1/session/ 的写路径 —— 用「把 title 设成它当前的值」做幂等验证（不改数据）
//  (2) /session/<startMs> 深链能不能直接打开 Session Info 面板（给 add_task 脚本用）
import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true, viewport: { width: 1600, height: 900 } });
const page = ctx.pages()[0] || (await ctx.newPage());
await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(9000);

const r = await page.evaluate(async () => {
  const db = await new Promise((res, rej) => { const q = indexedDB.open("firebaseLocalStorageDb"); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
  const all = await new Promise((res, rej) => { const tx = db.transaction("firebaseLocalStorage", "readonly"); const q = tx.objectStore("firebaseLocalStorage").getAll(); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
  const tok = all.map((x) => x?.value?.stsTokenManager?.accessToken).filter(Boolean)[0];
  const H = { Authorization: tok, "Content-Type": "application/json" };

  const listRes = await fetch("https://api.focusmate.com/v1/meetings/bookings", { headers: H });
  const list = await listRes.json();
  const now = Date.now();
  const future = list.meetings.filter((m) => m.meetingType === "paired" && m.startMs > now).sort((a, b) => a.startMs - b.startMs);
  const target = future[0];
  if (!target) return { err: "no future meeting" };

  // (1) 幂等 PUT：title 设成当前值
  const putRes = await fetch("https://api.focusmate.com/v1/session/", {
    method: "PUT", headers: H,
    body: JSON.stringify({ data: { sessionTime: target.startMs, title: target.title ?? "" } }),
  });
  const putText = await putRes.text();

  // 复核
  const list2 = await (await fetch("https://api.focusmate.com/v1/meetings/bookings", { headers: H })).json();
  const after = list2.meetings.find((m) => m.startMs === target.startMs);

  return {
    target: { startMs: target.startMs, id: target.id, title: target.title, quiet: target.preferences?.quietMode?.value },
    putStatus: putRes.status,
    putBody: putText.slice(0, 200),
    afterTitle: after?.title,
  };
});
console.log("=== (1) PUT 写路径（幂等） ===");
console.log(JSON.stringify(r, null, 1));

// (2) 深链
if (r.target) {
  await page.goto(`https://app.focusmate.com/session/${r.target.startMs}`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(8000);
  const panel = await page.evaluate(() => {
    const txt = document.body.innerText || "";
    return {
      url: location.href,
      hasSessionAt: /Session at /i.test(txt),
      hasAddTask: /Add task/i.test(txt),
      hasTitleInput: !!document.querySelector('input[placeholder="Session title..."]'),
      snippet: txt.slice(0, 400),
    };
  });
  console.log("=== (2) 深链 /session/<startMs> ===");
  console.log(JSON.stringify(panel, null, 1));
}
await ctx.close();
process.exit(0);
