#!/usr/bin/env node
/**
 * 把已订 session 切成 / 切回 quiet mode（走内部 API）。
 *
 * 实测映射（docs/focusmate-ui-api-notes.md §4.3）：
 *   UI:  Session Info 面板 → "Session settings" → Quiet Mode 开关
 *   API: PUT /v1/session/  { data: { sessionTime, preferences: { favorites:{value}, quietMode:{value} }, activityType } }
 *
 * 注意：quietMode / favorites / activityType 是一组，PUT 会整体覆盖，
 *       所以脚本先读出当前 favorites + activityType 再原样带回去，避免把它们冲掉。
 *
 * 用法：
 *   node scripts/set_quiet.mjs --start 18:00            # 开 quiet mode
 *   node scripts/set_quiet.mjs --start 18:00 --off      # 关
 *   node scripts/set_quiet.mjs --start 18:00 --dry-run
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
    `用法: node scripts/set_quiet.mjs --start HH:MM [--off] [--date YYYY-MM-DD] [--dry-run] [--headed]`
  );
  process.exit(0);
}

const startStr = requireArg(args, "start", "如 --start 18:00");
const wantQuiet = !args.off; // 默认开
const target = resolveStart(startStr, args.date);
const dryRun = !!args["dry-run"];

log(`目标 session：本地 ${fmtLocal(target)}`);
log(`quiet mode → ${wantQuiet ? "ON" : "OFF"}${dryRun ? "  [dry-run]" : ""}`);

if (dryRun) {
  // 注意：favorites / activityType 要读当前 session 才知道，dry-run 不连网所以用占位符
  emitJson({
    dryRun: true,
    method: "PUT",
    path: "/session/",
    body: {
      data: {
        sessionTime: target.getTime(),
        preferences: {
          favorites: { value: "<保持当前值>" },
          quietMode: { value: wantQuiet },
        },
        activityType: "<保持当前值>",
      },
    },
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

  const favorites = meeting.preferences?.favorites?.value ?? "noPreference";
  const activityType = meeting.activityType ?? 100;
  const body = {
    data: {
      sessionTime: meeting.startMs,
      preferences: { favorites: { value: favorites }, quietMode: { value: wantQuiet } },
      activityType,
    },
  };

  const res = await api(page, { method: "PUT", path: "/session/", token, body });
  if (res.status !== 200) {
    emitJson({ ok: false, step: "put", status: res.status, body: res.text.slice(0, 500) });
    die(`设置 quiet mode 失败：HTTP ${res.status} ${res.text.slice(0, 300)}`);
  }

  const after = await listMeetings(page, token);
  const now = findMeeting(after, target);
  const nowQuiet = now?.preferences?.quietMode?.value;
  const ok = nowQuiet === wantQuiet;

  log(
    `复核：quiet=${nowQuiet} favorites=${now?.preferences?.favorites?.value} activity=${now?.activityType}` +
      ` → ${ok ? "✅" : "❌"}`
  );
  emitJson({
    ok,
    action: "set_quiet_mode",
    sessionId: meeting.id,
    sessionTime: meeting.startMs,
    quietMode: nowQuiet,
    favorites,
    activityType,
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
