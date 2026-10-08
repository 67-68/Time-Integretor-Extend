// 诊断：dump 右侧面板里 "Add task" 那个元素的真实结构。
import { openSession, getToken, listMeetings, findMeeting, fmtTimeLabel } from "../scripts/_lib.mjs";

const target = (() => {
  const now = new Date();
  const t = new Date(now);
  t.setHours(20, 0, 0, 0);
  return t;
})();

const { ctx, page } = await openSession({ headless: true });
try {
  const token = await getToken(page);
  const meetings = await listMeetings(page, token);
  const m = findMeeting(meetings, target);
  if (!m) throw new Error("no meeting");
  const startLabel = fmtTimeLabel(new Date(m.startMs));
  const re = new RegExp(startLabel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b");
  const tile = page.locator("fm-booked-session-tile").filter({ hasText: re }).first();
  await tile.scrollIntoViewIfNeeded().catch(() => {});
  const box = await tile.boundingBox();
  await page.mouse.click(box.x + box.width - 30, box.y + Math.round(box.height * 0.35));
  await page.waitForTimeout(4500);

  const info = await page.evaluate(() => {
    const hits = [];
    for (const el of document.querySelectorAll("*")) {
      const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();
      if (/^add task$/i.test(own) || /add task/i.test(own)) {
        hits.push({
          tag: el.tagName,
          cls: (el.className || "").toString().slice(0, 90),
          ownText: own.slice(0, 40),
          clickable: el.tagName === "BUTTON" || el.getAttribute("role") === "button" || !!el.onclick,
          rect: (() => { const r = el.getBoundingClientRect(); return `${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.width)}x${Math.round(r.height)}`; })(),
          parentTag: el.parentElement?.tagName,
          parentCls: (el.parentElement?.className || "").toString().slice(0, 90),
        });
      }
    }
    return hits;
  });
  console.log(JSON.stringify(info, null, 1));

  // 试试几种定位器
  for (const [name, loc] of [
    ["role exact", page.getByRole("button", { name: /^Add task$/ })],
    ["getByText exact", page.getByText("Add task", { exact: true })],
    ["text regex", page.getByText(/Add task/i)],
    ["p-button hasText", page.locator("p-button").filter({ hasText: /^Add task$/ })],
  ]) {
    console.log(`locator ${name}: count=${await loc.count().catch((e) => "ERR:" + e.message.slice(0, 50))}`);
  }
} finally {
  await ctx.close().catch(() => {});
}
process.exit(0);
