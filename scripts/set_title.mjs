#!/usr/bin/env node
/**
 * 给已订的 session 设置 title（= 右侧 Session Info 面板上铅笔按钮那件事，走内部 API）。
 *
 * 实测映射（docs/focusmate-ui-api-notes.md §4.1）：
 *   UI:  点 session → 铅笔 / "Add a session title..." → 输入 → Enter
 *   API: PUT /v1/session/  { data: { sessionTime, title } }
 *        sessionTime = meeting.startMs
 *
 * 用法：
 *   node scripts/set_title.mjs --start 18:00 --title "写论文"
 *   node scripts/set_title.mjs --start 18:00 --title "" --date 2026-10-09
 *   node scripts/set_title.mjs --start 18:00 --title "写论文" --dry-run
 *
 * 退出码：0 成功 / 1 失败 / 2 参数错
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
} from "./_lib.mjs";

const args = parseArgs();

if (args.help || args.h) {
  console.log(
    `用法: node scripts/set_title.mjs --start HH:MM --title "..." [--date YYYY-MM-DD] [--dry-run] [--headed]`
  );
  process.exit(0);
}

const startStr = requireArg(args, "start", "如 --start 18:00");
if (args.title === undefined) die(`缺少 --title（可以给空串 "" 来清空）`, 2);
const title = String(args.title);
if (title.length > 100) {
  die(`title 最长 100 字符（Focusmate 输入框 maxlength=100），现在是 ${title.length}`, 2);
}

const target = resolveStart(startStr, args.date);
const dryRun = !!args["dry-run"];

log(`目标 session：本地 ${fmtLocal(target)}`);
log(`新 title：${JSON.stringify(title)}${dryRun ? "  [dry-run]" : ""}`);

if (dryRun) {
  emitJson({
    dryRun: true,
    method: "PUT",
    path: "/session/",
    body: { data: { sessionTime: target.getTime(), title } },
  });
  process.exit(0);
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

  const res = await api(page, {
    method: "PUT",
    path: "/session/",
    token,
    body: { data: { sessionTime: meeting.startMs, title } },
  });

  if (res.status !== 200) {
    emitJson({ ok: false, step: "put", status: res.status, body: res.text.slice(0, 500) });
    die(`设置 title 失败：HTTP ${res.status} ${res.text.slice(0, 300)}`);
  }

  // 复核：PUT 响应会回显更新后的 session；再拉一次列表交叉核对（API 才是 ground truth）
  const echoed = Array.isArray(res.json) ? res.json[0] : res.json;
  const after = await listMeetings(page, token);
  const now = findMeeting(after, target);
  const ok = now?.title === title;

  log(`复核：title = ${JSON.stringify(now?.title)} → ${ok ? "✅ 一致" : "❌ 不一致"}`);
  emitJson({
    ok,
    action: "set_title",
    sessionId: meeting.id,
    sessionTime: meeting.startMs,
    title,
    verified: ok,
    echoedTitle: echoed?.title,
  });
  if (!ok) exitCode = 1;
} catch (e) {
  log(`意外错误：${e?.stack || e}`);
  exitCode = 1;
} finally {
  await ctx.close().catch(() => {});
}

process.exit(exitCode);
