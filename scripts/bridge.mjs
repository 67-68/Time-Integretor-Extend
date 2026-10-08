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
 *   FOCUSMATE_PROFILE_DIR     profile 目录（默认 ~/.focusmate-mcp/browser-data）
 *   WARDEN_BRIDGE_IDLE_MS     空闲关浏览器的毫秒数，0 = 永不关
 *   WARDEN_BRIDGE_HEADED      1 = 有头模式（看操作过程 / 调试）
 *   PLAYWRIGHT_MODULE         playwright 模块路径（_lib 用）
 *
 * 退出码：0 正常 / 1 fatal（playwright 装不上等）
 */

import readline from "readline";

// ---------------------------------------------------------------- 独占 stdout

// 真正的 stdout 句柄，只有 send() 能用
const realOut = process.stdout.write.bind(process.stdout);
// 之后所有 console.log / process.stdout.write（含 _lib 的 log）都改道 stderr
process.stdout.write = (chunk, enc, cb) => process.stderr.write(chunk, enc, cb);

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

// ---------------------------------------------------------------- 命令

const handlers = {
  async ping() {
    return {
      pong: true,
      pid: process.pid,
      browser: browserUp(),
      tokenExp: token ? jwtExpMs(token) : 0,
      profileDir: lib.PROFILE_DIR,
      headless: HEADLESS,
      idleMs: IDLE_MS,
      uptimeMs: Date.now() - startedAt,
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

function handle(req) {
  queue.p = queue.p.then(async () => {
    const id = req && req.id !== undefined ? req.id : null;
    const cmd = req && req.cmd;
    const t0 = Date.now();
    inCommand = true;
    try {
      const fn = handlers[cmd];
      if (!fn) throw new Error("未知命令：" + JSON.stringify(cmd));
      const result = await fn(req);
      send({ id, ok: true, cmd, ms: Date.now() - t0, result });
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      send({ id, ok: false, cmd, ms: Date.now() - t0, error: msg });
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
  profileDir: lib.PROFILE_DIR,
  headless: HEADLESS,
  idleMs: IDLE_MS,
  idleAutoClose: IDLE_MS > 0,
});
