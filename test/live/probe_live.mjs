/**
 * probe_live.mjs —— 集成测试的**独立探针**：只看 Focusmate 真的有什么。
 *
 * 为什么不复用 warden 自己的对账代码：那样是「同一份假设验自己」。这里直接
 * 读 Focusmate 内部 API，把事实原样吐成 JSON，由 python 侧断言：
 *
 *   GET /v1/meetings/bookings  → 真实订上了哪些场（startMs / id / meetingType）
 *   GET /v1/tasks              → 真实挂上了哪些 task（title / attachments）
 *
 * 只读：不发任何 POST/PUT/DELETE。走 `scripts/_lib.mjs` 的 openSession，
 * 也就自动遵守 profile 跨进程锁（和 daemon/TUI 不会双开同一个 Chromium）。
 *
 *   FOCUSMATE_CONFIG_DIR=<home> node test/live/probe_live.mjs
 *   → {"meetings":[...],"tasks":[...]}
 */
import { openSession, getToken, api, listMeetings } from "../../scripts/_lib.mjs";

function die(obj, code = 1) {
  process.stdout.write(JSON.stringify(obj, null, 2) + "\n");
  process.exit(code);
}

let ctx;
try {
  const opened = await openSession({ headless: true, role: "live-probe" });
  ctx = opened.ctx;
  const page = opened.page;
  const token = await getToken(page);
  const meetings = await listMeetings(page, token);
  const r = await api(page, { path: "/tasks", token });
  if (r.status !== 200) {
    die({ error: `GET /v1/tasks HTTP ${r.status}`, text: String(r.text || "").slice(0, 300) });
  }
  const tasks = (r.json && (r.json.tasks || r.json.data || r.json)) || [];
  process.stdout.write(JSON.stringify({
    ok: true,
    now: Date.now(),
    meetings: meetings.map((m) => ({
      id: m.id,
      startMs: m.startMs,
      // 内部 API 用 durationMs（我们最开始读 m.duration 读成了 0）
      duration: m.durationMs ? Math.round(m.durationMs / 60000) : m.duration,
      meetingType: m.meetingType,
      title: m.title || "",
      activityType: m.activityType,
    })),
    tasks: (Array.isArray(tasks) ? tasks : []).map((t) => ({
      id: t.id,
      title: t.title || "",
      completedAt: t.completedAt || null,
      attachments: (t.attachments || []).map((a) => ({
        type: a.type,
        externalId: a.externalId,
      })),
    })),
  }) + "\n");
} catch (e) {
  die({ error: String((e && e.stack) || e) });
} finally {
  // ctx.close() 里挂了 release，profile 锁会跟着放掉
  if (ctx) await ctx.close().catch(() => {});
}
process.exit(0);
