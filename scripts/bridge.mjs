#!/usr/bin/env node
/**
 * bridge.mjs —— 常驻操作桥，给 warden.py（或任何前台程序）用。
 *
 * 存在的理由
 * ----------
 * scripts/ 下那四个脚本每次调用都要冷启动一个 Chromium（~10-15s）。
 * 一个 TUI 里连着敲 `mute 3` / `title 2 "..."` 这样根本没法用。
 * 所以这里把浏览器**留在内存里**，命令走 stdin/stdout 的 JSON 行协议，
 * 单条命令降到 ~1-2s。
 *
 * 协议
 * ----
 *   stdin   每行一个 JSON 请求：  {"id":1,"cmd":"snapshot"}
 *   stdout  每行一个 JSON 响应：  {"id":1,"ok":true,"cmd":"snapshot","ms":842,"result":{...}}
 *   有两类帧：
 *     - 响应帧：有 `id` / `ok`
 *     - 事件帧：有 `event`，没有 `id`（ready / idle-closed / fatal）
 *
 *   命令：
 *     ping                             探活
 *     snapshot [date=YYYY-MM-DD]       列某天（默认今天）的 session
 *     title    startMs meetingId? title       改标题
 *     mute     startMs meetingId? quiet       开/关 quiet mode
 *     cancel   startMs meetingId? source?     取消（**破坏性**，调用方负责把关）
 *     tasks                             列 Task Manager 的 task（含 completedAt）
 *     delete_task  taskId              删一条 task（GET 复核真没了）——
 *                                      任务转移走之后，旧场次上那条 task 用它清掉
 *     complete_task taskId             把一条 task 勾完成（API 候选 + GET 复核
 *                                      completedAt）；收尾「缺漏完成补充」用它
 *     add_task startMs title? notes? via? 给某场 session 挂 Focusmate task
 *                                       （默认先走内部 API，DOM 兜底；都复核）
 *
 *   ⚠️ 信封的 `id`（请求号）和「哪一场 session」**必须**用不同的字段名。
 *   踩过：两者都叫 id 时，Python 侧 `req.update(kw)` 会把请求号覆盖成 UUID，
 *   桥照常执行、响应也发了，但调用方永远等不到自己那个请求号 —— 表现为「卡死」，
 *   而写入其实已经生效。所以「哪一场」一律叫 meetingId。
 *     release                          只关浏览器、进程留着（把 profile 让给 daemon）
 *     shutdown                         关浏览器并退出
 *
 * 关键实现约束（踩过坑，别改）
 * ---------------------------
 * 1. **stdout 被独占**：_lib.mjs 里的 log()/console.log 会污染协议流，
 *    所以进程一起来就把 process.stdout.write 换成写 stderr，只留 send() 能写真 stdout。
 * 2. **_lib 的 die() 会 process.exit**：那是四个 CLI 脚本的语义（一错即退），
 *    对常驻进程是灾难 —— 一条命令失败不该带走整个桥。
 *    所以用 inCommand 标志把命令执行期间的 process.exit 转成异常。
 * 3. **串行执行**：浏览器和 profile 都不能并发碰，命令进队列一条条跑。
 * 4. 见上：**id 只做请求号**，session 身份用 meetingId。
 * 5. **空闲自动关浏览器**：daemon 也用同一个 Chromium profile，
 *    桥长期把浏览器挂着 = daemon 下单必然抢锁失败。
 *    空闲 IDLE_MS（默认 120s）就把浏览器关掉、释放锁，下次命令再冷启动。
 *
 * 环境变量：
 *   FOCUSMATE_CONFIG_DIR      运行目录（和 main.py / daemon 同一个；不传就按
 *                             scripts/mcp_home.mjs 的优先级自己找，见那里头注释）
 *   FOCUSMATE_PROFILE_DIR     profile 目录（点名，压过上面那个）
 *   WARDEN_BRIDGE_IDLE_MS     空闲关浏览器的毫秒数，0 = 永不关
 *   WARDEN_BRIDGE_HEADED      1 = 有头模式（看操作过程 / 调试）
 *   PLAYWRIGHT_MODULE         playwright 模块路径（_lib 用）
 *
 * 6. **`die()` 的真话要捡回来**：`_lib` 的 `die()` 先 `console.error` 再
 *    `process.exit`，转成异常以后只剩「内部调用了 process.exit(1)」这一句，
 *    真因（「未登录。先跑 --auth」）全在 stderr 里 —— 前端看到的等于没线索。
 *    所以这里把 stderr/stdout 都捞进环形缓冲，失败时把最后一条 `[ERROR]` 行
 *    拼进响应：`{"error": "...｜未登录（页面跳到了 /login）…", "detail": "..."}`。
 *
 * 退出码：0 正常 / 1 fatal（playwright 装不上等）
 */

import readline from "readline";
import fs from "node:fs";
import path from "node:path";
// delete_task 删除后要等几拍再 GET 复核。用 Node 内置的 promise 版 setTimeout，
// 不要引用 _lib.mjs：那个模块顶层会加载 Playwright，桥启动时不该被它拖住。
import { setTimeout as sleep } from "node:timers/promises";
import { attachTaskViaApi, completeTaskViaApi, mergeTaskLists } from "./task_attach.mjs";

// ---------------------------------------------------------------- 每日运行日志

// 与 Python 侧 runtime_log.py 同一个目录、同一套「按本地日期分文件」约定：
//   temp/logs/YYYY-MM-DD.jsonl
// 每行一个 JSON。日志是旁路：写不进去也绝不能影响命令。
const LOG_DIR = process.env.TI_LOG_DIR || path.join(process.cwd(), "temp", "logs");

function localDay(d = new Date()) {
  const pad = (n) => (n < 10 ? "0" + n : "" + n);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}

function logEvent(area, event, fields = {}) {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    const rec = {
      ts: new Date().toISOString(),
      area,
      event,
      ...fields,
    };
    fs.appendFileSync(path.join(LOG_DIR, localDay() + ".jsonl"),
      JSON.stringify(rec) + "\n", "utf8");
  } catch {
    /* 日志失败不能影响业务 */
  }
}

function taskLogRow(t) {
  return {
    id: t?.id,
    title: t?.title || "",
    completed: !!(t?.completed || t?.completedAt || t?.completed_at),
    completedAt: t?.completedAt || t?.completed_at || null,
    createdAt: t?.createdAt || t?.created_at || null,
    attachments: (t?.attachments || []).map((a) => ({
      type: a?.type || a?.attachmentType || "",
      externalId: a?.externalId ?? a?.id ?? "",
      startMs: a?.startMs ?? null,
    })),
  };
}

// ---------------------------------------------------------------- 独占 stdout

// 真正的 stdout 句柄，只有 send() 能用
const realOut = process.stdout.write.bind(process.stdout);
const realErr = process.stderr.write.bind(process.stderr);

// die() 的真话都在 stderr 上（console.error / warn / log），而 process.exit
// 一抛就只剩一句「内部调用了 process.exit(1)」。所以两边都抄一份进环形缓冲，
// 失败时把最后一条 [ERROR] 行拼回响应 —— 否则排查全靠去翻日志文件。
const LIBLOG_MAX = 120;
const libLog = [];
function remember(chunk) {
  try {
    for (const line of String(chunk).split("\n")) {
      const s = line.trim();
      if (!s) continue;
      libLog.push(s);
      if (libLog.length > LIBLOG_MAX) libLog.shift();
    }
  } catch {
    /* 记日志本身失败不许影响命令 */
  }
}
// 之后所有 console.log / process.stdout.write（含 _lib 的 log）都改道 stderr
process.stdout.write = (chunk, enc, cb) => {
  remember(chunk);
  return realErr(chunk, enc, cb);
};
process.stderr.write = (chunk, enc, cb) => {
  remember(chunk);
  return realErr(chunk, enc, cb);
};

/** 最后一条像「原因」的行：优先 [ERROR]，没有就空着。 */
function libDetail() {
  for (let i = libLog.length - 1; i >= 0; i--) {
    if (libLog[i].includes("[ERROR]")) {
      return libLog[i].replace(/^\[focusmate\](\[ERROR\])?\s*/, "").trim();
    }
  }
  return "";
}

/** 这句错是不是「登录态没了」——决定要不要缓存住，别把浏览器空转 20 遍。 */
function isAuthError(text) {
  return /未登录|登录态|Firebase token|token 已过期|重新登录|\/login|AUTH_REQUIRED/.test(
    String(text || "")
  );
}

function send(obj) {
  realOut(JSON.stringify(obj) + "\n");
}

// ---------------------------------------------------------------- die() 兜底

let inCommand = false;
const realExit = process.exit.bind(process);
process.exit = (code) => {
  if (inCommand) {
    // _lib 的 die() 语义 == 这条命令失败，不该杀掉常驻进程
    const e = new Error("内部调用了 process.exit(" + code + ")，已转成命令级失败");
    e.__libExit = code;
    throw e;
  }
  realExit(code);
};

process.on("uncaughtException", (e) => {
  send({ event: "fatal", error: String(e && e.stack ? e.stack : e) });
  realExit(1);
});
process.on("unhandledRejection", (e) => {
  send({ event: "fatal", error: String(e && e.stack ? e.stack : e) });
  realExit(1);
});

// ---------------------------------------------------------------- 载入 _lib

let lib;
try {
  lib = await import("./_lib.mjs");
} catch (e) {
  send({ event: "fatal", error: "载入 _lib.mjs 失败：" + String((e && e.message) || e) });
  realExit(1);
}

const HEADLESS = process.env.WARDEN_BRIDGE_HEADED !== "1";
const IDLE_MS = Number(process.env.WARDEN_BRIDGE_IDLE_MS || 120000);

// ---------------------------------------------------------------- 状态

let ctx = null;
let page = null;
let token = null;
let idleTimer = null;
let startedAt = Date.now();

function browserUp() {
  return !!(ctx && page);
}

function jwtExpMs(tok) {
  try {
    const payload = tok.split(".")[1];
    const json = JSON.parse(Buffer.from(payload, "base64").toString("utf8"));
    return typeof json.exp === "number" ? json.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

async function closeBrowser(reason) {
  if (!ctx) return;
  const c = ctx;
  ctx = null;
  page = null;
  token = null;
  await c.close().catch(() => {});
  send({ event: "idle-closed", reason: reason || "idle", at: Date.now() });
}

function resetIdle() {
  if (idleTimer) {
    clearTimeout(idleTimer);
    idleTimer = null;
  }
  if (!IDLE_MS || IDLE_MS <= 0) return;
  idleTimer = setTimeout(() => {
    idleTimer = null;
    if (browserUp()) closeBrowser("idle").catch(() => {});
  }, IDLE_MS);
  // 别让这个 timer 拖住事件循环
  if (idleTimer.unref) idleTimer.unref();
}

async function ensureBrowser() {
  if (browserUp()) return;
  const s = await lib.openSession({ headless: HEADLESS });
  ctx = s.ctx;
  page = s.page;
  token = null;
}

async function getTok() {
  await ensureBrowser();
  if (token && jwtExpMs(token) > Date.now() + 60000) return token;
  token = await lib.getToken(page);
  return token;
}

/** 调内部 API；401 / TOKEN_REFRESH_CODES 时重新取 token 重试一次。 */
async function callApi(method, path, body) {
  let tok = await getTok();
  let r = await lib.api(page, { method, path, token: tok, body });
  const code = r.json && r.json.code;
  if (r.status === 401 || (code && [70, 17, 35].includes(code))) {
    token = null;
    tok = await getTok();
    r = await lib.api(page, { method, path, token: tok, body });
  }
  return r;
}

async function loadMeetings() {
  const r = await callApi("GET", "/meetings/bookings");
  if (r.status !== 200) {
    throw new Error("列 session 失败：HTTP " + r.status + " " + String(r.text || "").slice(0, 200));
  }
  return (r.json && r.json.meetings) || [];
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function localDayRange(dateStr) {
  const now = new Date();
  let y = now.getFullYear();
  let m = now.getMonth();
  let d = now.getDate();
  if (dateStr) {
    const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr));
    if (!dm) throw new Error("date 要 YYYY-MM-DD，收到 " + JSON.stringify(dateStr));
    y = Number(dm[1]);
    m = Number(dm[2]) - 1;
    d = Number(dm[3]);
  }
  const start = new Date(y, m, d, 0, 0, 0, 0).getTime();
  const end = new Date(y, m, d, 23, 59, 59, 999).getTime();
  return {
    start,
    end,
    label: y + "-" + pad(m + 1) + "-" + pad(d),
  };
}

function shapeMeeting(m) {
  const s = new Date(m.startMs);
  const e = new Date(m.startMs + m.durationMs);
  return {
    startMs: m.startMs,
    endMs: m.startMs + m.durationMs,
    startLabel: pad(s.getHours()) + ":" + pad(s.getMinutes()),
    endLabel: pad(e.getHours()) + ":" + pad(e.getMinutes()),
    dayLabel: s.getFullYear() + "-" + pad(s.getMonth() + 1) + "-" + pad(s.getDate()),
    durationMin: Math.round(m.durationMs / 60000),
    id: m.id,
    title: m.title || "",
    quiet: !!(m.preferences && m.preferences.quietMode && m.preferences.quietMode.value),
    favorites: (m.preferences && m.preferences.favorites && m.preferences.favorites.value) || null,
    activityType: m.activityType,
    activityLabel: lib.ACTIVITY_TYPES[m.activityType] || String(m.activityType),
    partnerId: m.partnerId || null,
    matched: !!m.partnerId,
  };
}

/** 按 startMs 找一场；同时校验 id（防止调用方手上的 ID 已经错位）。 */
async function requireMeeting(startMs, id) {
  if (typeof startMs !== "number" || !isFinite(startMs)) {
    throw new Error("startMs 必须是数字，收到 " + JSON.stringify(startMs));
  }
  const meetings = await loadMeetings();
  const m = meetings.find((x) => x.meetingType === "paired" && x.startMs === startMs);
  if (!m) {
    throw new Error("找不到 startMs=" + startMs + " 的 session（可能已被取消，或时间已变）");
  }
  if (id && m.id !== id) {
    throw new Error(
      "session 身份对不上：调用方以为 id=" + id + "，实际是 " + m.id + "。已中止，什么都没改。"
    );
  }
  return { meeting: m, meetings };
}

// ---------------------------------------------------------------- task helpers

function normalizeTask(t) {
  return {
    id: t.id,
    title: t.title || "",
    description: t.description || "",
    completed: !!(t.completed || t.completedAt || t.completed_at),
    completedAt: t.completedAt || t.completed_at || null,
    createdAt: t.createdAt || t.created_at || null,
    attachments: (t.attachments || []).map((a) => ({
      type: a.type || a.attachmentType || "",
      externalId: a.externalId ?? a.id ?? "",
    })),
  };
}

function tasksFromResponse(r, endpoint) {
  if (r.status !== 200) {
    logEvent("focusmate", "tasks_pull_page_error", {
      endpoint,
      status: r.status,
      error: String(r.text || "").slice(0, 1000),
    });
    throw new Error(
      "GET " + endpoint + " 失败：HTTP " + r.status + " " + String(r.text || "").slice(0, 300)
    );
  }
  const arr = r.json?.tasks ?? (Array.isArray(r.json) ? r.json : r.json?.data ?? []);
  const rows = Array.isArray(arr) ? arr : [];
  logEvent("focusmate", "tasks_pull_page", {
    endpoint,
    status: r.status,
    count: rows.length,
    hasMore: !!r.json?.hasMore,
    nextCursor: r.json?.nextCursor ?? null,
    tasks: rows.map(taskLogRow),
  });
  return rows;
}

/** 未完成的 task —— GET /tasks 只返回这一半（Focusmate 的完成项不在这里）。 */
async function listIncompleteTasksRaw() {
  return tasksFromResponse(await callApi("GET", "/tasks"), "/tasks");
}

/** 已完成的 task —— 单独一页一页拉；GET /tasks 看不到它们。 */
async function listCompletedTasksRaw() {
  const out = [];
  let cursor = null;
  for (let page = 0; page < 50; page++) {
    const path = "/tasks/completed" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : "");
    const r = await callApi("GET", path);
    out.push(...tasksFromResponse(r, "/tasks/completed"));
    if (!r.json?.hasMore || !r.json?.nextCursor) break;
    cursor = r.json.nextCursor;
  }
  return out;
}

/**
 * 两个列表合在一起。
 *
 * 「关联任务完成之后没同步」的真因：Focusmate 的 GET /tasks 只列**未完成**
 * 的 task，勾完的会从那里消失、只在 GET /tasks/completed 出现。以前只读
 * /tasks → 本地永远看不到 completedAt → 不会写回 OmniFocus。
 */
async function listAllTasksRaw() {
  const started = Date.now();
  logEvent("focusmate", "tasks_pull_start", { source: "/tasks + /tasks/completed" });
  let completedFailed = null;
  const [active, completed] = await Promise.all([
    listIncompleteTasksRaw(),
    listCompletedTasksRaw().catch((e) => {
      // 老服务端/临时故障时退回只看未完成，不把整条命令拖死。
      completedFailed = String(e && e.message ? e.message : e);
      logEvent("focusmate", "tasks_pull_completed_error", { error: completedFailed });
      process.stderr.write("[focusmate][WARN] 拉已完成 task 失败：" + brief(e) + "\n");
      return [];
    }),
  ]);
  const merged = mergeTaskLists(active, completed);
  logEvent("focusmate", "tasks_pull_done", {
    source: "/tasks + /tasks/completed",
    active_count: active.length,
    completed_count: completed.length,
    total: merged.length,
    completed_fetch_failed: completedFailed,
    elapsed_ms: Date.now() - started,
    tasks: merged.map(taskLogRow),
  });
  return merged;
}

/** 只给 attach/复核用：新建中的 task 必在未完成列表里。 */
async function listTasksRaw() {
  return listIncompleteTasksRaw();
}

async function pickTextLocator(candidates) {
  for (const loc of candidates) {
    const n = await loc.count().catch(() => 0);
    if (n > 0) {
      const first = loc.first();
      await first.waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
      return first;
    }
  }
  return null;
}

/** 这一场 session 上现在挂着哪些 task（GET /tasks 里按 attachment 过滤）。 */
async function tasksForMeeting(meetingId) {
  if (!meetingId) return [];
  const all = await listTasksRaw();
  return all
    .filter((t) => (t.attachments || []).some((a) => (a.externalId ?? a.id) === meetingId))
    .map(normalizeTask);
}

function brief(e) {
  return String((e && e.message) || e).replace(/\s+/g, " ").slice(0, 200);
}

async function deleteTaskQuiet(taskId) {
  if (!taskId) return;
  try {
    await callApi("DELETE", "/tasks/" + encodeURIComponent(taskId), {});
  } catch {
    /* 清理失败就算了：它只是没挂上的孤儿，报错里已经说明了 */
  }
}

/**
 * 走内部 API 给 session 建 task 并挂上（不碰 DOM）。
 *
 * 存在的理由：**这场 session 已经有 task 时，右侧面板里就没有 "Add task" 了**，
 * DOM 路径必然找不到按钮（用户报的「已经有 task 时无法加入额外的 task」）。
 * API 侧没这个限制：task ↔ session 是 n:n（`attachments` 是数组）。
 * 具体怎么试 body 形状、怎么复核、怎么回滚，都在 scripts/task_attach.mjs 里
 * （纯逻辑，能单测；这里只把真实的 callApi 传进去）。
 */
async function createTaskViaApi(meeting, title, notes, attempts) {
  try {
    const out = await attachTaskViaApi(
      { callApi, tasksForMeeting, deleteTask: deleteTaskQuiet },
      meeting, title, notes
    );
    attempts.push(...out.attempts);
    return out.task;
  } catch (e) {
    if (Array.isArray(e.attempts)) attempts.push(...e.attempts);
    throw e;
  }
}

/** 走真实 UI 给 session 挂 task，然后用 GET /tasks 复核 attachment。 */
async function addTaskViaDom(meeting, title, notes) {
  const start = new Date(meeting.startMs);
  const p2 = (n) => String(n).padStart(2, "0");
  // 2026-10-23 后看板改成 24 小时制；旧 12 小时制卡片也要兼容。
  const start24 = `${p2(start.getHours())}:${p2(start.getMinutes())}`;
  const startOld = lib.fmtTimeLabel(start);
  const esc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const startRe = new RegExp(`(?:${esc(start24)}|${esc(startOld)})\\b`);
  const tile = page.locator("fm-booked-session-tile").filter({ hasText: startRe }).first();
  if ((await tile.count()) === 0) {
    throw new Error(`页面上找不到起点 ${start24} 的 session 卡片`);
  }
  await tile.scrollIntoViewIfNeeded().catch(() => {});
  const box = await tile.boundingBox();
  if (!box) throw new Error("session 卡片没有 boundingBox（不可见？）");
  await page.mouse.click(box.x + box.width - 30, box.y + Math.round(box.height * 0.35));
  await page.waitForTimeout(4000);

  const panelText = await page.locator("body").innerText().catch(() => "");
  const panelMatches = panelText.includes(`Session at ${start24}`) ||
                       panelText.includes(`Session at ${startOld}`);
  if (!panelMatches) {
    throw new Error(`右侧面板不是 ${start24} 那一场；什么都没改`);
  }
  const addTask = await pickTextLocator([
    page.getByText("Add task", { exact: true }),
    page.getByRole("button", { name: /^Add task$/ }),
    page.getByText(/^Add task$/),
  ]);
  if (!addTask) {
    const already = await tasksForMeeting(meeting.id).catch(() => []);
    // 面板正文带一小段出来：下次真机排查（按钮改名 / 挪进抽屉 / Labs 关了）一眼能看出来
    const snippet = String(panelText || "").replace(/\s+/g, " ").trim().slice(0, 400);
    if (already.length) {
      throw new Error(
        '面板里没有 "Add task" —— 这场 session 已经有 ' + already.length + " 个 task（" +
          already.map((t) => t.title).filter(Boolean).join(" / ") +
          "），UI 就不再给入口了；这种事走内部 API（bridge 默认路径）。" +
          (snippet ? " 面板正文：" + snippet : "")
      );
    }
    throw new Error('面板里找不到 "Add task"（Task Manager Labs 关掉了？）' +
                    (snippet ? " 面板正文：" + snippet : ""));
  }
  await addTask.click();
  const titleBox = page.locator('textarea[placeholder="Task title"]').first();
  await titleBox.waitFor({ state: "visible", timeout: 8000 });
  await titleBox.fill(title);
  if (notes) {
    const notesBox = page.locator('textarea[placeholder^="Notes"]').first();
    if ((await notesBox.count()) > 0) await notesBox.fill(notes);
  }
  const createBtn = await pickTextLocator([
    page.getByText("Create task", { exact: true }),
    page.getByRole("button", { name: /^Create task$/i }),
    page.getByText(/^Create task$/i),
  ]);
  if (!createBtn) throw new Error('找不到 "Create task" 按钮');
  await createBtn.click();
  await page.waitForTimeout(5000);
  const all = await listTasksRaw();
  const created = all.find(
    (t) => (t.title ?? "") === title &&
      (t.attachments || []).some((a) => (a.externalId ?? a.id) === meeting.id)
  );
  return created || null;
}

// ---------------------------------------------------------------- 命令

const handlers = {
  async ping() {
    return {
      pong: true,
      pid: process.pid,
      browser: browserUp(),
      tokenExp: token ? jwtExpMs(token) : 0,
      home: lib.CONFIG_DIR,
      profileDir: lib.PROFILE_DIR,
      profileSource: lib.PROFILE_SOURCE,
      headless: HEADLESS,
      idleMs: IDLE_MS,
      uptimeMs: Date.now() - startedAt,
    };
  },

  /** 「你到底在读哪个 profile」—— 排查登录态/多 profile 的第一句（不开浏览器）。 */
  async where() {
    const p = await handlers.ping();
    return {
      home: p.home,
      profileDir: p.profileDir,
      profileSource: p.profileSource,
      browser: p.browser,
      tokenExp: p.tokenExp,
      tokenLive: !!p.tokenExp && p.tokenExp > Date.now(),
      lastAuthError: lastAuth ? { at: lastAuth.at, error: lastAuth.error } : null,
      logTail: libLog.slice(-8),
    };
  },

  async snapshot(req) {
    const range = localDayRange(req.date);
    const meetings = await loadMeetings();
    const rows = meetings
      .filter((m) => m.meetingType === "paired")
      .filter((m) => m.startMs >= range.start && m.startMs <= range.end)
      .sort((a, b) => a.startMs - b.startMs)
      .map(shapeMeeting);
    return { date: range.label, now: Date.now(), count: rows.length, sessions: rows };
  },

  async title(req) {
    const title = String(req.title == null ? "" : req.title);
    if (title.length > 100) {
      throw new Error("title 最长 100 字符（Focusmate 输入框 maxlength=100），现在是 " + title.length);
    }
    const { meeting } = await requireMeeting(req.startMs, req.meetingId);
    const r = await callApi("PUT", "/session/", {
      data: { sessionTime: meeting.startMs, title },
    });
    if (r.status !== 200) {
      throw new Error("设置 title 失败：HTTP " + r.status + " " + String(r.text || "").slice(0, 300));
    }
    const after = await loadMeetings();
    const now = after.find((x) => x.meetingType === "paired" && x.startMs === meeting.startMs);
    const verified = !!(now && (now.title || "") === title);
    return { verified, sessionId: meeting.id, startMs: meeting.startMs, title, actual: (now && now.title) || "" };
  },

  async mute(req) {
    const want = !!req.quiet;
    const { meeting } = await requireMeeting(req.startMs, req.meetingId);
    const favorites = (meeting.preferences && meeting.preferences.favorites && meeting.preferences.favorites.value) || "noPreference";
    const activityType = meeting.activityType == null ? 100 : meeting.activityType;
    const r = await callApi("PUT", "/session/", {
      data: {
        sessionTime: meeting.startMs,
        preferences: { favorites: { value: favorites }, quietMode: { value: want } },
        activityType,
      },
    });
    if (r.status !== 200) {
      throw new Error("设置 quiet mode 失败：HTTP " + r.status + " " + String(r.text || "").slice(0, 300));
    }
    const after = await loadMeetings();
    const now = after.find((x) => x.meetingType === "paired" && x.startMs === meeting.startMs);
    const actual = !!(now && now.preferences && now.preferences.quietMode && now.preferences.quietMode.value);
    return {
      verified: actual === want,
      sessionId: meeting.id,
      startMs: meeting.startMs,
      quiet: actual,
      favorites,
      activityType,
    };
  },

  async tasks() {
    const all = await listAllTasksRaw();
    const rows = all.map(normalizeTask);
    return { count: rows.length, tasks: rows };
  },

  async add_task(req) {
    const title = String(req.title == null ? "" : req.title).trim();
    if (!title) throw new Error("task title 不能为空");
    const notes = req.notes == null ? "" : String(req.notes);
    if (title.length > 500) throw new Error("task title 最长 500 字符，现在是 " + title.length);
    const { meeting } = await requireMeeting(req.startMs, req.meetingId);
    const existing = await tasksForMeeting(meeting.id).catch(() => []);
    const attempts = [];
    // auto（默认）= 先 API 再 DOM；api / dom = 只用那条路（排查用）
    const via = req.via ? String(req.via) : "auto";
    let created = null;
    let usedVia = null;
    if (via !== "dom") {
      try {
        created = await createTaskViaApi(meeting, title, notes, attempts);
        if (created) usedVia = "api";
      } catch (e) {
        attempts.push("API：" + brief(e));
      }
    }
    if (!created && via !== "api") {
      try {
        created = await addTaskViaDom(meeting, title, notes);
        if (created) usedVia = "dom";
        else attempts.push("DOM：UI 上点完了，但 GET /tasks 里没看到这条 attachment");
      } catch (e) {
        attempts.push("DOM：" + brief(e));
      }
    }
    if (!created) {
      throw new Error("没能挂上 task。" + (attempts.length ? "试过：" + attempts.join("；") : ""));
    }
    const normalized = normalizeTask(created);
    return {
      verified: true,
      via: usedVia,
      attempts,
      existingCount: existing.length,
      existing: existing.map((t) => ({ id: t.id, title: t.title })),
      sessionId: meeting.id,
      sessionTime: meeting.startMs,
      title,
      notes,
      taskId: normalized.id,
      task: normalized,
    };
  },

  async complete_task(req) {
    const taskId = String(req.taskId || "");
    if (!taskId) throw new Error("complete_task 要 taskId（Focusmate task 的 id）");
    const started = Date.now();
    const attempts = [];
    logEvent("focusmate", "task_complete_start", { taskId });
    try {
      const done = await completeTaskViaApi(
        {
          callApi,
          findTask: async (id) => {
            const all = await listAllTasksRaw();
            const hit = all.find((t) => String(t.id) === String(id));
            return hit ? normalizeTask(hit) : null;
          },
        },
        taskId
      );
      attempts.push(...(done.attempts || []));
      const task = normalizeTask(done.task || {});
      const result = {
        verified: !!task.completedAt,
        already: !!done.already,
        via: done.via || null,
        taskId,
        task,
        completedAt: task.completedAt,
        attempts,
      };
      logEvent("focusmate", "task_complete_result", {
        ...result,
        elapsed_ms: Date.now() - started,
      });
      return result;
    } catch (e) {
      attempts.push(...((e && e.attempts) || []));
      logEvent("focusmate", "task_complete_error", {
        taskId,
        attempts,
        error: String(e && e.message ? e.message : e),
        elapsed_ms: Date.now() - started,
      });
      const err = new Error((e && e.message ? e.message : String(e)) +
        (attempts.length ? "｜试过：" + attempts.join("；") : ""));
      err.attempts = attempts;
      throw err;
    }
  },

  async delete_task(req) {
    // 删一条 Focusmate task（Task Manager 的实体，不是 session）。
    // 用途：任务**转移**走之后，旧那一场 session 上挂着的那条 task 就没意义了
    // —— 它既不会完成、也不该继续出现在 Task Manager 里。收尾 A/E 两步会调它。
    const taskId = String(req.taskId || "");
    if (!taskId) throw new Error("delete_task 要 taskId（Focusmate task 的 id）");
    let before = null;
    try {
      const all = await listAllTasksRaw();
      before = all.find((t) => String(t.id) === taskId) || null;
    } catch (e) {
      // 列不出来也继续删：删完还会复核
    }
    const r = await callApi("DELETE", "/tasks/" + encodeURIComponent(taskId), {});
    if (![200, 201, 202, 204, 404].includes(r.status)) {
      throw new Error(
        "删除 task 失败：HTTP " + r.status + " " + String(r.text || "").slice(0, 300)
      );
    }
    // 删掉的东西必须以「GET /tasks 里没有它」为准 —— 接口回 200 不等于真没了。
    let still = null;
    for (let i = 0; i < 3; i++) {
      await sleep(i === 0 ? 300 : 800);
      let all = [];
      try {
        all = await listAllTasksRaw();
      } catch (e) {
        continue;
      }
      still = all.find((t) => String(t.id) === taskId) || null;
      if (!still) break;
    }
    return {
      verified: !still,
      taskId,
      alreadyGone: !before && !still,
      deleted: before ? normalizeTask(before) : null,
      stillThere: still ? normalizeTask(still) : null,
    };
  },

  async cancel(req) {
    const source = req.source ? String(req.source) : "nowSessionsSidebar";
    if (!lib.CANCEL_SOURCES.includes(source)) {
      throw new Error("source 不在已知枚举里：" + source);
    }
    const { meeting } = await requireMeeting(req.startMs, req.meetingId);
    const before = shapeMeeting(meeting);
    const path =
      "/session/" + encodeURIComponent(meeting.id) + "?source=" + encodeURIComponent(source);
    const r = await callApi("DELETE", path, {});
    if (r.status !== 200 && r.status !== 204) {
      throw new Error("取消失败：HTTP " + r.status + " " + String(r.text || "").slice(0, 300));
    }
    const after = await loadMeetings();
    const still = after.find((x) => x.meetingType === "paired" && x.startMs === meeting.startMs);
    return { verified: !still, cancelled: before, source };
  },

  async release() {
    // 把浏览器关掉、进程留着。给「马上要启 daemon」用——
    // daemon 下单也要独占同一个 Chromium profile，桥挂着浏览器它就抢不到锁。
    const was = browserUp();
    await closeBrowser("release");
    return { released: was, browser: browserUp() };
  },

  async shutdown() {
    await closeBrowser("shutdown");
    setTimeout(() => realExit(0), 50);
    return { bye: true };
  },
};

// ---------------------------------------------------------------- 主循环

const queue = { p: Promise.resolve() };

/**
 * 登录态刚失败过：短时间内的后续命令**直接回同一句话**，不再冷启动 Chromium。
 *
 * 踩过：profile 选错时每条命令都要花 15s 开浏览器 → 进 /login → die()，
 * TUI 里连敲十来下就是十来次白等，日志里刷出 20 条一模一样的「未登录」。
 * 登录态不可能在 60s 内自己好，所以这 60s 里直接告诉人「先 --auth」。
 */
const AUTH_COOLDOWN_MS = 60000;
let lastAuth = null;
//: 这些命令本身就是在查救护信息 / 收尾，不该被冷却挡住。
const BYPASS_COOLDOWN = new Set(["ping", "where", "release", "shutdown"]);

function handle(req) {
  queue.p = queue.p.then(async () => {
    const id = req && req.id !== undefined ? req.id : null;
    const cmd = req && req.cmd;
    const t0 = Date.now();
    inCommand = true;
    libLog.length = 0; // 这一条命令的日志，别混上一条的
    try {
      if (!BYPASS_COOLDOWN.has(cmd) && lastAuth && Date.now() - lastAuth.at < AUTH_COOLDOWN_MS) {
        const left = Math.ceil((AUTH_COOLDOWN_MS - (Date.now() - lastAuth.at)) / 1000);
        send({
          id,
          ok: false,
          cmd,
          ms: 0,
          error: lastAuth.error + `（刚失败过，${left}s 内不再开浏览器重试）`,
          detail: `刚失败过，${left}s 内不再开浏览器重试`,
          cached: true,
        });
        return;
      }
      const fn = handlers[cmd];
      if (!fn) throw new Error("未知命令：" + JSON.stringify(cmd));
      const result = await fn(req);
      lastAuth = null; // 有命令成功了，登录态至少目前是活的
      send({ id, ok: true, cmd, ms: Date.now() - t0, result });
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      // die() 转成异常以后只剩「内部调用了 process.exit(1)」，真因在 stderr。
      // 有真因就**替掉**那句噪音，别让前端同时读两句废话。
      const detail = libDetail();
      const noise = /^内部调用了 process\.exit\(/.test(msg);
      let error = msg;
      if (detail && !msg.includes(detail)) error = noise ? detail : msg + "｜" + detail;
      if (isAuthError(error)) {
        lastAuth = { at: Date.now(), cmd, error };
        // 这个浏览器已经是登录页了，留着只会继续误导下一条命令
        await closeBrowser("auth-failed").catch(() => {});
      }
      send({
        id,
        ok: false,
        cmd,
        ms: Date.now() - t0,
        error,
        detail,
        exitCode: (e && e.__libExit) || 0,
      });
    } finally {
      inCommand = false;
      resetIdle();
    }
  });
  return queue.p;
}

const rl = readline.createInterface({ input: process.stdin });

rl.on("line", (line) => {
  const s = line.trim();
  if (!s) return;
  let req;
  try {
    req = JSON.parse(s);
  } catch (e) {
    send({ event: "protocol-error", error: "JSON 解析失败：" + e.message, raw: s.slice(0, 200) });
    return;
  }
  handle(req);
});

let closing = false;
rl.on("close", async () => {
  // stdin 被关了（TUI 退出）——把浏览器带干净，别留 Chromium 孤儿。
  // 注意要**先等队列跑完**：stdin 关闭和"正在执行的那条命令"是并发的，
  // 直接退会把跑到一半的命令连同浏览器一起带走（早期版本就这么丢过响应）。
  if (closing) return;
  closing = true;
  if (idleTimer) clearTimeout(idleTimer);
  await queue.p.catch(() => {});
  await closeBrowser("stdin-closed").catch(() => {});
  realExit(0);
});

process.on("SIGTERM", async () => {
  if (idleTimer) clearTimeout(idleTimer);
  await closeBrowser("sigterm").catch(() => {});
  realExit(0);
});
process.on("SIGINT", async () => {
  if (idleTimer) clearTimeout(idleTimer);
  await closeBrowser("sigint").catch(() => {});
  realExit(0);
});

resetIdle();
send({
  event: "ready",
  pid: process.pid,
  home: lib.CONFIG_DIR,
  profileDir: lib.PROFILE_DIR,
  profileSource: lib.PROFILE_SOURCE,
  headless: HEADLESS,
  idleMs: IDLE_MS,
  idleAutoClose: IDLE_MS > 0,
});
