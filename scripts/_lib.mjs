/**
 * scripts 下四个 session 操作脚本共用的底座。
 *
 * 设计要点（都来自实测，见 docs/focusmate-ui-api-notes.md）：
 * - 复用 focusmate-mcp 的持久化浏览器 profile（~/.focusmate-mcp/browser-data），
 *   所以不需要重新登录；与 MCP/daemon 共用同一个 profile，别并发跑。
 * - 内部 API（https://api.focusmate.com/v1/）用 **裸 Firebase ID token** 鉴权，
 *   不是 `Bearer <token>`：实测裸 JWT → 200，`Bearer x` → 401 code 71。
 * - token 从页面 IndexedDB 的 firebaseLocalStorageDb 里读，
 *   过期时靠 app 自己刷新（reload 一次即可）。
 * - 所有请求都在页面上下文里 fetch，指纹 = 官方前端。
 */
import { createRequire } from "module";
import path from "path";
import { REPO_ROOT, resolveProfile } from "./mcp_home.mjs";
import * as LOCK from "./profile_lock.mjs";

const __dirname = path.dirname(new URL(import.meta.url).pathname);

// ---------------------------------------------------------------- playwright

function loadPlaywright() {
  const req = createRequire(import.meta.url);
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    // 与 focusmate-mcp 同版本，浏览器构建才对得上
    path.join(REPO_ROOT, "temp", "focusmate-mcp", "node_modules", "playwright"),
    "playwright",
  ].filter(Boolean);
  const errors = [];
  for (const c of candidates) {
    try {
      return req(c);
    } catch (e) {
      errors.push(`${c}: ${e.code || e.message}`);
    }
  }
  console.error("[focusmate][ERROR] 找不到 playwright，试过：\n  " + errors.join("\n  "));
  console.error("提示：先跑 ./install.sh / npm i（在 temp/focusmate-mcp 里），或用 PLAYWRIGHT_MODULE 指定路径。");
  process.exit(1);
}

export const { chromium } = loadPlaywright();

// ---------------------------------------------------------------- constants

export const API_BASE = "https://api.focusmate.com/v1";
export const APP_URL = "https://app.focusmate.com";

// 运行目录 / profile 的解析只有一份实现（scripts/mcp_home.mjs）：
// 老代码这里写死 `FOCUSMATE_CONFIG_DIR || ~/.focusmate-mcp`，而 warden 起
// bridge 时不传那个变量 —— 于是 daemon 用工作区 profile、bridge 用 9 月的老
// profile，每条命令都「未登录」。见那个文件的头注释。
const _home = resolveProfile();
export const CONFIG_DIR = _home.home;
export const PROFILE_DIR = _home.profileDir;
/** 这个 profile 是怎么选出来的（env:profile / workspace / legacy …），报错时带上。 */
export const PROFILE_SOURCE = _home.source;

/** 一句话说清「用的是哪个 profile、怎么来的」—— 所有 die() 都带它。 */
export function describeProfile() {
  return `profile=${PROFILE_DIR}（${PROFILE_SOURCE}）`;
}

/** session 面板里 "My Task" 的活动类型（实测枚举）。 */
export const ACTIVITY_TYPES = { 100: "Anything", 200: "Desk", 400: "Moving" };

/** cancel 的 ?source= 取值（实测是 app 的枚举，仅用于埋点）。 */
export const CANCEL_SOURCES = [
  "nowSessionsSidebar",
  "dynamicPanelMySessions",
  "dynamicPanelSessionInfo",
  "favoritesAvailabilityList",
  "individualAvailabilityList",
  "dynamicPanelFavoritesAvailabilityList",
  "dynamicPanelIndividualAvailabilityList",
  "consolidatedFavoritesList",
  "seeAvailabilityList",
];

// ---------------------------------------------------------------- log / die

export function log(...a) {
  console.log("[focusmate]", ...a);
}
export function warn(...a) {
  console.warn("[focusmate][warn]", ...a);
}
export function die(msg, code = 1) {
  console.error("[focusmate][ERROR]", msg);
  process.exit(code);
}

// ---------------------------------------------------------------- CLI

/**
 * 极简参数解析：`--k v` / `--k=v` / `--flag`（无值 = true）。
 * 位置参数进 `_`。
 */
export function parseArgs(argv = process.argv.slice(2)) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) {
      out._.push(a);
      continue;
    }
    const body = a.slice(2);
    const eq = body.indexOf("=");
    if (eq >= 0) {
      out[body.slice(0, eq)] = body.slice(eq + 1);
      continue;
    }
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      out[body] = next;
      i++;
    } else {
      out[body] = true;
    }
  }
  return out;
}

export function requireArg(args, name, hint) {
  const v = args[name];
  if (v === undefined || v === true || v === "") die(`缺少 --${name}${hint ? "（" + hint + "）" : ""}`, 2);
  return String(v);
}

// ---------------------------------------------------------------- 时间

/**
 * 把 `--start HH:MM`（+ 可选 `--date YYYY-MM-DD`）解析成本地 Date。
 * 语义与 main.py 一致：默认指今天，已过则顺延到明天；必须是 15 分钟整点。
 */
export function resolveStart(input, dateStr) {
  const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(String(input).trim());
  if (!m) die(`时间格式不对，要 24 小时制 "HH:MM"，如 14:50；收到的是 ${JSON.stringify(input)}`, 2);
  const hour = Number(m[1]);
  const minute = Number(m[2]);
  if (minute % 15 !== 0) {
    die(`Focusmate 只认 15 分钟整点（:00/:15/:30/:45），${input} 不行`, 2);
  }

  const now = new Date();
  let day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (dateStr) {
    const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr));
    if (!dm) die(`--date 格式要 YYYY-MM-DD，收到 ${JSON.stringify(dateStr)}`, 2);
    day = new Date(Number(dm[1]), Number(dm[2]) - 1, Number(dm[3]));
  }
  const target = new Date(day);
  target.setHours(hour, minute, 0, 0);
  if (!dateStr && target <= now) target.setDate(target.getDate() + 1);
  return target;
}

/** "4:30pm" —— dashboard session 卡片上的时间标签格式。 */
export function fmtTimeLabel(d) {
  let h = d.getHours();
  const ampm = h >= 12 ? "pm" : "am";
  h = h % 12;
  if (h === 0) h = 12;
  return `${h}:${String(d.getMinutes()).padStart(2, "0")}${ampm}`;
}

export function fmtLocal(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------------------------------------------------------------- 浏览器

/** 这一进程当前持有的 profile 锁（同一时刻只有一个浏览器，够用）。 */
let _heldLock = null;

// die()/process.exit 也要把锁放掉：不然一次登录失败就把 profile 永久锁死。
process.once("exit", () => {
  if (_heldLock) {
    try {
      LOCK.release(CONFIG_DIR, _heldLock);
    } catch {
      /* ignore */
    }
    _heldLock = null;
  }
});

/** 包一层 ctx.close：关浏览器的同时放锁，调用方不用记得。 */
function wrapClose(ctx) {
  const raw = ctx.close.bind(ctx);
  let closed = false;
  ctx.close = async (...args) => {
    if (closed) return;
    closed = true;
    try {
      await raw(...args);
    } finally {
      if (_heldLock) {
        LOCK.release(CONFIG_DIR, _heldLock);
        _heldLock = null;
      }
    }
  };
  return ctx;
}

/** 强制放锁：`ctx.close()` 卡死时由 bridge 兜底调用。 */
export function releaseProfileLock() {
  if (!_heldLock) return false;
  const ok = LOCK.release(CONFIG_DIR, _heldLock);
  _heldLock = null;
  return ok;
}

/** 杀掉占着本 profile 的 Chromium（锁在自己手上时才是安全的）。 */
export function killProfileBrowsers() {
  return LOCK.killProfileBrowsers(PROFILE_DIR, { quiet: true });
}

/** 有没有人在等这个 profile（daemon 下单前会写 want）。 */
export function wantPending() {
  return LOCK.wantPending(CONFIG_DIR);
}

/** 给人看的一行：现在谁持锁（`where` / 看板排查用）。 */
export function profileLockInfo() {
  const info = LOCK.readLock(CONFIG_DIR);
  return info
    ? { ...info, text: LOCK.describeHolder(info), mine: Number(info.pid) === process.pid }
    : null;
}

/** 谁在等着要这个 profile。 */
export function profileWantInfo() {
  const info = LOCK.wantPending(CONFIG_DIR);
  return info ? { ...info, text: `${info.role}（pid ${info.pid}）在等` } : null;
}

/**
 * 起浏览器、进 dashboard，返回 { ctx, page }。
 *
 * **先拿 profile 锁再开浏览器**：同一个 user-data-dir 被两个 Chromium 同时
 * 打开会白屏，并在争抢中打坏 IndexedDB（见 profile_lock.mjs 头注释）。
 * 锁在 ctx.close() 时自动放掉（wrapClose），所以调用方照旧只写 ctx.close()。
 */
export async function openSession({ headless = true, viewport, role } = {}) {
  const lockRole = role || process.env.FOCUSMATE_LOCK_ROLE || "cli";
  const waitMs = Number(process.env.FOCUSMATE_LOCK_WAIT_MS || LOCK.DEFAULT_WAIT_MS);
  try {
    _heldLock = LOCK.acquire(CONFIG_DIR, lockRole, { waitMs, quiet: true });
  } catch (e) {
    if (e instanceof LOCK.ProfileBusyError) {
      die(
        `profile 被 ${LOCK.describeHolder(e.holder)} 占着，等了 ` +
          `${(e.waitedMs / 1000).toFixed(1)}s 还没让开（这一条先跳过，稍后重试）。` +
          `${describeProfile()}`
      );
    }
    throw e;
  }

  let ctx;
  try {
    ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
      headless,
      viewport: viewport || { width: 1600, height: 900 },
    });
  } catch (e) {
    LOCK.release(CONFIG_DIR, _heldLock);
    _heldLock = null;
    die(`起不来 Chromium：${e.message}${describeProfile()}`);
  }
  wrapClose(ctx);

  const page = ctx.pages()[0] || (await ctx.newPage());
  try {
    await page.goto(`${APP_URL}/dashboard`, { waitUntil: "domcontentloaded" });
  } catch (e) {
    await ctx.close().catch(() => {});
    die(`打不开 Focusmate：${e.message}（网络？或 profile 被别的进程占用？${describeProfile()}）`);
  }
  // Firebase 有长连接，networkidle 等不到；给 SPA 渲染 + 刷新 token 的时间
  await page.waitForTimeout(7000);

  if (page.url().includes("/login")) {
    await ctx.close().catch(() => {});
    die(
      `未登录（页面跳到了 /login）。先跑 \`python3 main.py --auth\` 登录一次；` +
        `要是那边登录过还这样，就是 profile 选错了：${describeProfile()}`
    );
  }

  // 白屏 = SPA 没挂载：profile 写不进去，或另一只浏览器正占着它。
  // 以前这里没检查，下游 locator 要白等 10s 再报一句「找不到 Book 按钮」。
  const blank = await page
    .evaluate(() => ((document.body && document.body.innerText) || "").trim().length === 0)
    .catch(() => false);
  if (blank) {
    await ctx.close().catch(() => {});
    die(
      `dashboard 白屏（SPA 没挂载，bodyLen=0）——profile 写不进去，或另一只浏览器` +
        `正在用它。${describeProfile()}`
    );
  }
  return { ctx, page };
}

// ---------------------------------------------------------------- token

function jwtExpMs(token) {
  try {
    const payload = token.split(".")[1];
    const json = JSON.parse(Buffer.from(payload, "base64").toString("utf8"));
    return typeof json.exp === "number" ? json.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

async function readToken(page) {
  return page.evaluate(async () => {
    const openDb = (name) =>
      new Promise((res, rej) => {
        const q = indexedDB.open(name);
        q.onsuccess = () => res(q.result);
        q.onerror = () => rej(q.error);
      });
    try {
      const db = await openDb("firebaseLocalStorageDb");
      const all = await new Promise((res, rej) => {
        const tx = db.transaction("firebaseLocalStorage", "readonly");
        const q = tx.objectStore("firebaseLocalStorage").getAll();
        q.onsuccess = () => res(q.result);
        q.onerror = () => rej(q.error);
      });
      const tok = all
        .map((r) => r?.value?.stsTokenManager?.accessToken)
        .filter(Boolean)[0];
      return tok || null;
    } catch {
      return null;
    }
  });
}

/**
 * 页面现在是不是「登录页」。
 *
 * 为什么先问这一句再重载：登录态失效时，三次 reload × 5s 等下来要几十秒才报错
 * （实测一条 task pull 跑了 57 秒），而这几十秒里页面一直停在 /login —— reload
 * 一万次也变不出 token。看出来就立刻 die，把「去登录」这句话直接还给人。
 */
async function looksLoggedOut(page) {
  try {
    const url = String(page.url() || "");
    if (/\/(login|signin|signup|auth)\b/i.test(url)) return true;
    // 密码框比邮箱框可靠：应用页面里也可能出现 type=email 的输入（设置/搜索），
    // 误判成「未登录」会直接 die，比慢十几秒更糟。
    return await page.evaluate(
      () => !!document.querySelector('input[type="password"]'),
    );
  } catch (e) {
    return false;
  }
}

/** 拿一个没过期的 token；过期就让页面自己刷新（reload）。 */
export async function getToken(page, { attempts = 3 } = {}) {
  for (let i = 0; i < attempts; i++) {
    const tok = await readToken(page);
    if (tok && jwtExpMs(tok) > Date.now() + 30_000) return tok;
    if (await looksLoggedOut(page)) {
      die(`拿不到 Firebase token：页面停在登录页（profile 里没有登录态），先跑一次登录。${describeProfile()}`);
    }
    log(`token ${tok ? "即将/已过期" : "读不到"}，让页面刷新一次（${i + 1}/${attempts}）…`);
    await page.reload({ waitUntil: "domcontentloaded" }).catch(() => {});
    await page.waitForTimeout(5000);
  }
  const tok = await readToken(page);
  if (!tok) die(`拿不到 Firebase token：profile 里没有登录态，先跑一次登录。${describeProfile()}`);
  if (jwtExpMs(tok) <= Date.now())
    die(`token 已过期且自动刷新失败，请重新登录一次。${describeProfile()}`);
  return tok;
}

// ---------------------------------------------------------------- 内部 API

/**
 * 在页面上下文里调内部 API。
 * method: GET / PUT / POST / DELETE；path 形如 "/session/" 或 "/session/<id>?source=x"。
 * body 为 undefined 时不发 body、不带 Content-Type。
 */
export async function api(page, { method = "GET", path, body, token }) {
  return page.evaluate(
    async ({ url, method, token, body }) => {
      const headers = { Authorization: token };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      let res;
      try {
        res = await fetch(url, {
          method,
          headers,
          body: body !== undefined ? JSON.stringify(body) : undefined,
        });
      } catch (e) {
        return { status: 0, error: String(e), text: "", json: null };
      }
      const text = await res.text();
      let json = null;
      try {
        json = JSON.parse(text);
      } catch {
        /* 非 JSON 就留 null */
      }
      return { status: res.status, json, text };
    },
    { url: API_BASE + path, method, token, body }
  );
}

/** GET /v1/meetings/bookings —— 已订 session 列表（含 preferences / activityType）。 */
export async function listMeetings(page, token) {
  const r = await api(page, { path: "/meetings/bookings", token });
  if (r.status !== 200) {
    die(`列 session 失败：HTTP ${r.status} ${r.text.slice(0, 200)}`);
  }
  return r.json?.meetings || [];
}

/**
 * 按本地时间找 session。
 * 注意：meeting.startMs 就是 App 里的 sessionTime，也是 PUT /session/ 的 key。
 */
export function findMeeting(meetings, target) {
  const want = target.getTime();
  return meetings.find((m) => m.meetingType === "paired" && m.startMs === want) || null;
}

export function describeMeeting(m) {
  const start = new Date(m.startMs);
  const durMin = Math.round(m.durationMs / 60000);
  return (
    `${fmtLocal(start)} (${durMin}min) ` +
    `id=${m.id} title=${JSON.stringify(m.title)} ` +
    `quiet=${m.preferences?.quietMode?.value} ` +
    `activity=${ACTIVITY_TYPES[m.activityType] || m.activityType}`
  );
}

// ---------------------------------------------------------------- 杂

export function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** 打印 JSON 结果，便于上层脚本/agent 解析。 */
export function emitJson(obj) {
  console.log("RESULT_JSON " + JSON.stringify(obj));
}
