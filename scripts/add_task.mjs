#!/usr/bin/env node
/**
 * 给已订 session 加一个 task（= 右侧 Session Info 面板的 "Add task"，走 DOM 自动化）。
 *
 * 为什么这个走 DOM 而不是 API：Task Manager 是独立实体（title/description/category/
 * attachments），且是 Labs 功能，创建配额等逻辑都在前端，DOM 更稳。
 * 实测流程（docs/focusmate-ui-api-notes.md §4.2）：
 *   点 session 卡片 → 右侧 Session Info 面板 → "Add task"
 *   → textarea[placeholder="Task title"] 填内容
 *   → 点 "Create task"
 *
 * 踩坑：整块卡片的点击绑在根 div 上，但 avatar / 时间 / partner 名都会 stopPropagation，
 *       所以必须点卡片的空白位置（脚本用「右侧、上三分之一」这个实测有效的落点）。
 *
 * 用法：
 *   node scripts/add_task.mjs --start 18:00 --title "写论文"
 *   node scripts/add_task.mjs --start 18:00 --title "写论文" --notes "先写 intro"
 *   node scripts/add_task.mjs --start 18:00 --title "写论文" --dry-run
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
  fmtTimeLabel,
  fmtLocal,
  log,
  die,
  emitJson,
} from "./_lib.mjs";

const args = parseArgs();

if (args.help || args.h) {
  console.log(
    `用法: node scripts/add_task.mjs --start HH:MM --title "Task title" [--notes "..."] [--date YYYY-MM-DD] [--dry-run] [--headed]`
  );
  process.exit(0);
}

/**
 * PrimeNG 的 p-button 文字在 <span class="p-button-label"> 里，getByRole 有时匹配不到，
 * 所以按顺序试多个定位器，返回第一个 count>0 的。
 */
async function pickLocator(candidates) {
  for (const loc of candidates) {
    const n = await loc.count().catch(() => 0);
    if (n > 0) {
      const first = loc.first();
      await first.waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
      return { loc: first, via: loc.toString().slice(0, 60) };
    }
  }
  return null;
}

const startStr = requireArg(args, "start", "如 --start 18:00");
const title = requireArg(args, "title", "task 的标题，不能为空");
const notes = args.notes === undefined ? "" : String(args.notes);
const target = resolveStart(startStr, args.date);
const dryRun = !!args["dry-run"];

log(`目标 session：本地 ${fmtLocal(target)}`);
log(`task：title=${JSON.stringify(title)} notes=${JSON.stringify(notes)}${dryRun ? "  [dry-run]" : ""}`);

if (dryRun) {
  emitJson({ dryRun: true, action: "add_task", sessionTime: target.getTime(), title, notes });
  process.exit(0);
}

const { ctx, page } = await openSession({ headless: !args.headed });
let exitCode = 0;
try {
  // --- 先用 API 定位 session，拿到准确时长（卡片上的时间段标签要用） ---
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

  const start = new Date(meeting.startMs);
  const end = new Date(meeting.startMs + meeting.durationMs);
  const startLabel = fmtTimeLabel(start); // 卡片上比如 "8:00pm"（进行中的卡片是 "4:30pm - 5:20pm"）
  const rangeLabel = `${startLabel} - ${fmtTimeLabel(end)}`;
  const startRe = new RegExp(startLabel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b");
  log(`在页面上找卡片：起点 ${JSON.stringify(startLabel)}（进行中的卡片显示 ${JSON.stringify(rangeLabel)}）`);

  const tile = page.locator("fm-booked-session-tile").filter({ hasText: startRe }).first();
  if ((await tile.count()) === 0) {
    const labels = await page
      .locator("fm-booked-session-tile")
      .allInnerTexts()
      .catch(() => []);
    die(
      `页面上找不到起点 ${startLabel} 的卡片。\n  可见卡片：${JSON.stringify(
        labels.map((t) => t.replace(/\s+/g, " ").slice(0, 60))
      )}`
    );
  }

  await tile.scrollIntoViewIfNeeded().catch(() => {});
  const box = await tile.boundingBox();
  if (!box) die("卡片没有 boundingBox（不可见？）");

  // 点「右侧 + 上 35%」的空位：避开 avatar(左)、时间/名字(会 stopPropagation)、
  // 以及右下角的 Join / More options / Cancel 按钮
  await page.mouse.click(box.x + box.width - 30, box.y + Math.round(box.height * 0.35));
  await page.waitForTimeout(4000);

  // --- 关键安全闸：先确认打开的面板就是这一场，再动任何写操作 ---
  const panelText = await page.locator("body").innerText().catch(() => "");
  const panelMatches = panelText.includes(`Session at ${startLabel}`);
  if (!panelMatches) {
    die(
      `点了卡片，但右侧面板不是 ${startLabel} 那一场（没看到 "Session at ${startLabel}"）。\n` +
        `可能点错了卡片 / 面板没打开。什么都没改，请人工确认。\n` +
        `页面片段：\n${panelText.slice(0, 600)}`
    );
  }
  log(`面板已打开且是目标场次（"Session at ${startLabel}"）✅`);

  // --- 右侧面板应出现 "Add task" ---
  // 踩坑：PrimeNG 的 p-button 里文字在 <span class="p-button-label">，
  // getByRole('button',{name}) 有时匹配不到，所以用 getByText 优先、role 兜底。
  const addTask = await pickLocator([
    page.getByText("Add task", { exact: true }),
    page.getByRole("button", { name: /^Add task$/ }),
    page.getByText(/^Add task$/),
  ]);
  if (!addTask) {
    die(
      `面板开了，但找不到 "Add task" 按钮。\n` +
        `可能原因：这场已经挂过 task（显示 "Task for this session"）/ Task Manager(Labs) 关掉了 / 表结构变了。\n` +
        `页面片段：\n${panelText.slice(0, 800)}`
    );
  }
  log(`找到 "Add task"（${addTask.via}）`);
  await addTask.loc.click();

  // --- 填表：Task title (+ 可选 notes) ---
  const titleBox = page.locator('textarea[placeholder="Task title"]').first();
  await titleBox.waitFor({ state: "visible", timeout: 8000 });
  await titleBox.fill(title);

  if (notes) {
    const notesBox = page.locator('textarea[placeholder^="Notes"]').first();
    if ((await notesBox.count()) > 0) {
      await notesBox.fill(notes);
    } else {
      log("没找到 notes 输入框，跳过（只填 title）。");
    }
  }

  // --- 提交 ---
  const createBtn = await pickLocator([
    page.getByText("Create task", { exact: true }),
    page.getByRole("button", { name: /^Create task$/i }),
    page.getByText(/^Create task$/i),
  ]);
  if (!createBtn) die('找不到 "Create task" 按钮，表单结构可能变了。');
  log(`点 "Create task"（${createBtn.via}）`);
  await createBtn.loc.click();
  log("已点击 Create task，等待结果…");
  await page.waitForTimeout(5000);

  // --- 复核：直接查 API 更可靠（UI 上表单创建后会切成编辑态，看不出成功与否） ---
  await page.waitForTimeout(3000);
  const tasksRes = await api(page, { path: "/tasks", token });
  const allTasks = tasksRes.json?.tasks ?? (Array.isArray(tasksRes.json) ? tasksRes.json : []);
  const created = allTasks.find(
    (t) =>
      (t.title ?? "") === title &&
      (t.attachments || []).some((a) => (a.externalId ?? a.id) === meeting.id)
  );

  const bodyText = await page.locator("body").innerText().catch(() => "");
  const sawToast = /task.*(added|created)|added to session/i.test(bodyText);

  const ok = !!created;
  log(
    `复核：API 里找到挂到本场的 task=${created ? `✅ id=${created.id}` : "❌ 没找到"}` +
      `${sawToast ? "（页面也有成功提示）" : ""}`
  );
  emitJson({
    ok,
    action: "add_task",
    sessionId: meeting.id,
    sessionTime: meeting.startMs,
    title,
    notes,
    taskId: created?.id ?? null,
    verified: ok,
  });
  if (!ok) {
    log("提示：task 可能建了但没挂到这场 session 上，去 Task Manager 确认一下。");
    exitCode = 1;
  }
} catch (e) {
  log(`意外错误：${e?.stack || e}`);
  exitCode = 1;
} finally {
  await ctx.close().catch(() => {});
}

process.exit(exitCode);
