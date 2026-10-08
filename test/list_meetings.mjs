// 只读：列出手上所有 session（本地时间 / 时长 / partner / title / quiet），
// 用来给 scripts/*.mjs 挑 --start 目标。
import { openSession, getToken, listMeetings, ACTIVITY_TYPES, fmtTimeLabel, log } from "../scripts/_lib.mjs";

const { ctx, page } = await openSession({ headless: true });
try {
  const token = await getToken(page);
  const meetings = await listMeetings(page, token);
  const now = Date.now();
  const rows = meetings
    .filter((m) => m.meetingType === "paired")
    .sort((a, b) => a.startMs - b.startMs)
    .map((m) => {
      const s = new Date(m.startMs);
      const e = new Date(m.startMs + m.durationMs);
      return {
        local: `${s.getFullYear()}-${String(s.getMonth() + 1).padStart(2, "0")}-${String(s.getDate()).padStart(2, "0")} ${fmtTimeLabel(s)}`,
        range: `${fmtTimeLabel(s)} - ${fmtTimeLabel(e)}`,
        startMs: m.startMs,
        dur: Math.round(m.durationMs / 60000),
        future: m.startMs > now,
        partner: m.partnerId ? "yes" : "-",
        title: m.title,
        quiet: m.preferences?.quietMode?.value,
        activity: ACTIVITY_TYPES[m.activityType] || m.activityType,
        id: m.id,
      };
    });
  console.log(JSON.stringify(rows, null, 1));
} finally {
  await ctx.close().catch(() => {});
}
process.exit(0);
