#!/usr/bin/env node
/**
 * 取消一个已订 session（= session 卡片右下角那个 × 按钮，走内部 API）。
 *
 * 实测映射（docs/focusmate-ui-api-notes.md §4.4）：
 *   UI:  卡片右下角 ×（`span.pi.pi-times`，accessible name "Cancel session: …"）
 *        没 partner 时点一下直接取消；有 partner 时会多一步二次确认
 *   API: DELETE /v1/session/{externalId}?source=<枚举>
 *        externalId = meeting.id（UUID），不是 startMs
 *
 * ⚠️ 破坏性操作：默认要显式 --yes 才真的取消。
 *
 * 用法：
 *   node scripts/cancel.mjs --start 18:00            # 只预览，不取消
 *   node scripts/cancel.mjs --start 18:00 --yes      # 真取消
 *
 * 退出码：0 成功 / 1 失败 / 2 参数错 / 3 需要 --yes
 */
import {
  parseArgs,
  requireArg,
  resolveStart,
  openSession,
  getToken,
  api,
  listMeetings,
  findMeeting,
  describeMeeting,
  fmtLocal,
  log,
  die,
  emitJson,
  CANCEL_SOURCES,
} from "./_lib.mjs";

const args = parseArgs();

if (args.help || args.h) {
  console.log(
    `用法: node scripts/cancel.mjs --start HH:MM [--yes] [--source <枚举>] [--date YYYY-MM-DD] [--headed]`
  );
  process.exit(0);
}

const startStr = requireArg(args, "start", "如 --start 18:00");
const target = resolveStart(startStr, args.date);
const source = args.source ? String(args.source) : "nowSessionsSidebar";
if (!CANCEL_SOURCES.includes(source)) {
  die(`--source 不在已知枚举里：${source}\n  已知值：${CANCEL_SOURCES.join(", ")}`, 2);
}

const { ctx, page } = await openSession({ headless: !args.headed });
let exitCode = 0;
try {
  const token = await getToken(page);
  const meetings = await listMeetings(page, token);
  const meeting = findMeeting(meetings, target);

  if (!meeting) {
    const upcoming = meetings
      .filter((m) => m.meetingType === "paired" && m.startMs > Date.now())
      .sort((a, b) => a.startMs - b.startMs)
      .slice(0, 12)
      .map(describeMeeting)
      .join("\n    ");
    log("未来已有的 session：\n    " + (upcoming || "（无）"));
    die(`没找到本地 ${fmtLocal(target)} 的 session。`);
  }

  log(`命中：${describeMeeting(meeting)}`);
  if (meeting.partnerId) {
    log(`注意：这场已经匹配到 partner（${meeting.partnerId}）。页面上会多一步二次确认。`);
  }

  if (!args.yes) {
    emitJson({
      ok: false,
      action: "cancel",
      wouldCancel: { sessionId: meeting.id, sessionTime: meeting.startMs, partnerId: meeting.partnerId || null },
      hint: "加上 --yes 才会真的取消",
    });
    log("这是预览（没加 --yes），什么都没改。");
    process.exit(3);
  }

  const path = `/session/${encodeURIComponent(meeting.id)}?source=${encodeURIComponent(source)}`;
  log(`DELETE ${path}`);
  const res = await api(page, { method: "DELETE", path, token, body: {} });

  // 200/204 都算成功
  if (res.status !== 200 && res.status !== 204) {
    emitJson({ ok: false, step: "delete", status: res.status, body: res.text.slice(0, 500) });
    die(`取消失败：HTTP ${res.status} ${res.text.slice(0, 300)}`);
  }

  const after = await listMeetings(page, token);
  const still = findMeeting(after, target);
  const ok = !still;

  log(`复核：该时段的 session ${still ? "仍然存在 ❌" : "已消失 ✅"}`);
  emitJson({
    ok,
    action: "cancel",
    sessionId: meeting.id,
    sessionTime: meeting.startMs,
    source,
    verified: ok,
  });
  if (!ok) exitCode = 1;
} catch (e) {
  log(`意外错误：${e?.stack || e}`);
  exitCode = 1;
} finally {
  await ctx.close().catch(() => {});
}

process.exit(exitCode);
