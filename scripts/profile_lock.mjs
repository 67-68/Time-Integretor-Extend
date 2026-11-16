/**
 * profile_lock.mjs —— Focusmate Chromium profile 的跨进程互斥锁（node 侧）。
 *
 * 和 `focusmate_lock.py` 是**同一份协议**，文件名、判据、TTL 全部对齐：
 *
 *   <home>/.profile-lock.json   {"pid":123,"role":"bridge","ts":123.4,"host":"…"}
 *   <home>/.profile-want.json   等锁的人写的「请让位」标记（20s 过期）
 *
 * 为什么需要（2026-11-16 真机实测）
 * -------------------------------
 * 同一个 user-data-dir 被两个 Chromium 同时打开时，第二个的 dashboard 会白屏
 * （`SPA did not mount`，bodyLen=0），IndexedDB 报
 * `Internal error opening backing store`。`chrome-headless-shell` **不写
 * SingletonLock**，Chromium 根本不拦第二个实例 —— 于是 daemon 每 5 分钟一轮的
 * 下单和 TUI 的常驻 bridge 天天撞，把 `firebase:authUser` 记录踩丢，表现就是
 * 「登录态天天腐烂，每天都要重新登录」。
 *
 * 约定：
 * - 谁要开浏览器谁先拿锁；拿不到就写 want，持锁方（bridge）看到会立刻关浏览器
 *   让位，所以正常等待是 1-2 秒。
 * - 持锁进程死了 / 超过 staleMs 没动静 → 视为死锁，直接抢。
 * - 抢到锁以后发现还有陌生 Chromium 开着这个 profile（泄漏的孤儿）→ 杀掉。
 */
import fs from "fs";
import os from "os";
import path from "path";
import { execFileSync } from "child_process";

export const LOCK_NAME = ".profile-lock.json";
export const WANT_NAME = ".profile-want.json";
export const DEFAULT_WAIT_MS = 90000; // 要盖住 daemon 一整轮下单（含 3 次重试）
export const DEFAULT_STALE_MS = 900000; // 15 分钟；auth 最长 5 分钟，留足
export const WANT_TTL_MS = 20000;
export const POLL_MS = 250;

/** 同步睡一会儿（node 里用 Atomics.wait，不占 CPU）。 */
export function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

export function lockPath(home) {
  return path.join(path.resolve(home), LOCK_NAME);
}
export function wantPath(home) {
  return path.join(path.resolve(home), WANT_NAME);
}

function readJson(p) {
  try {
    const d = JSON.parse(fs.readFileSync(p, "utf8"));
    return d && typeof d === "object" ? d : null;
  } catch {
    return null;
  }
}

export function readLock(home) {
  return home ? readJson(lockPath(home)) : null;
}

export function wantPending(home) {
  const info = home ? readJson(wantPath(home)) : null;
  if (!info) return null;
  const ts = Number(info.ts) || 0;
  return (Date.now() / 1000 - ts) * 1000 <= WANT_TTL_MS ? info : null;
}

export function clearWant(home) {
  if (!home) return;
  try {
    fs.unlinkSync(wantPath(home));
  } catch {
    /* 没有就算了 */
  }
}

export function pidAlive(pid) {
  const n = Number(pid);
  if (!Number.isInteger(n) || n <= 0) return false;
  try {
    process.kill(n, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM"; // 活着，只是不是我的
  }
}

export function holderIsStale(info, staleMs = DEFAULT_STALE_MS) {
  if (!info) return true;
  const ts = Number(info.ts) || 0;
  const ageMs = Date.now() - ts * 1000;
  if (ageMs > staleMs) return true;
  if (info.host && info.host !== os.hostname()) return false; // 别的机器按超时算
  return !pidAlive(info.pid);
}

export function describeHolder(info) {
  if (!info) return "没有进程持锁";
  const held = Math.max(0, Date.now() / 1000 - (Number(info.ts) || 0));
  return `pid ${info.pid} 的 ${info.role || "?"}（已持锁 ${held.toFixed(0)}s）`;
}
export class ProfileBusyError extends Error {
  constructor(holder, waitedMs) {
    super(`${describeHolder(holder)}（等了 ${(waitedMs / 1000).toFixed(1)}s）`);
    this.name = "ProfileBusyError";
    this.holder = holder || {};
    this.waitedMs = waitedMs;
  }
}

function writeWant(home, role, waitMs) {
  const info = { pid: process.pid, role, ts: Date.now() / 1000, host: os.hostname(), wait_ms: waitMs };
  const tmp = `${wantPath(home)}.tmp-${process.pid}`;
  try {
    fs.writeFileSync(tmp, JSON.stringify(info));
    fs.renameSync(tmp, wantPath(home));
  } catch {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* ignore */
    }
  }
}

/**
 * 这个 profile 目录的解析：`FOCUSMATE_PROFILE_DIR` 优先，否则 `<home>/browser-data`。
 */
export function profileDirFor(home) {
  const override = (process.env.FOCUSMATE_PROFILE_DIR || "").trim();
  if (override) return path.resolve(override.replace(/^~(?=$|\/)/, os.homedir()));
  return path.join(path.resolve(home), "browser-data");
}

/** 开着这个 profile 的 Chromium **主进程**（子进程带 --type=，跳过）。 */
export function findProfileBrowsers(profileDir) {
  const target = `--user-data-dir=${path.resolve(profileDir)}`;
  let out = "";
  try {
    out = execFileSync("pgrep", ["-f", "--", target], { encoding: "utf8" });
  } catch {
    return []; // pgrep 没匹配到会返回 1
  }
  const pids = [];
  for (const tok of out.split(/\s+/)) {
    if (!/^\d+$/.test(tok)) continue;
    let cmd = "";
    try {
      cmd = execFileSync("ps", ["-o", "command=", "-p", tok], { encoding: "utf8" });
    } catch {
      continue;
    }
    if (cmd.includes("--type=")) continue;
    pids.push(Number(tok));
  }
  return pids;
}

export function killProfileBrowsers(profileDir, { quiet = false } = {}) {
  const pids = findProfileBrowsers(profileDir);
  if (!pids.length) return [];
  for (const pid of pids) {
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      /* 已经死了 */
    }
  }
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline && pids.some((p) => pidAlive(p))) sleepSync(100);
  for (const pid of pids) {
    if (!pidAlive(pid)) continue;
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      /* ignore */
    }
  }
  if (!quiet) console.error(`[lock] 清掉占着 profile 的孤儿浏览器：${pids.join(" ")}`);
  return pids;
}

/**
 * 拿锁。拿不到抛 ProfileBusyError（带 holder，方便报「谁占着」）。
 */
export function acquire(home, role, {
  waitMs = DEFAULT_WAIT_MS,
  staleMs = DEFAULT_STALE_MS,
  pollMs = POLL_MS,
  quiet = false,
  killOrphans = true,
} = {}) {
  if (!home) return null; // 没指定运行目录就退化成「不上锁」，别把 CLI 弄死
  const dir = path.resolve(home);
  fs.mkdirSync(dir, { recursive: true });
  const lp = lockPath(dir);
  const started = Date.now();
  const deadline = started + waitMs;
  const me = { pid: process.pid, role, ts: Date.now() / 1000, host: os.hostname() };
  let poked = false;
  for (;;) {
    let fd = null;
    try {
      fd = fs.openSync(lp, "wx", 0o600);
    } catch (e) {
      if (e.code !== "EEXIST") throw e;
    }
    if (fd !== null) {
      try {
        fs.writeSync(fd, JSON.stringify(me));
      } finally {
        fs.closeSync(fd);
      }
      clearWant(dir);
      if (killOrphans) killProfileBrowsers(profileDirFor(dir), { quiet: true });
      if (poked && !quiet) {
        console.error(`[lock] 拿到 profile 锁（等了 ${((Date.now() - started) / 1000).toFixed(1)}s）`);
      }
      return me;
    }

    const holder = readLock(dir);
    if (holderIsStale(holder, staleMs)) {
      try {
        fs.unlinkSync(lp);
      } catch {
        /* 别人抢先删了 */
      }
      continue;
    }
    if (!poked) {
      writeWant(dir, role, waitMs);
      poked = true;
      if (!quiet) console.error(`[lock] profile 被 ${describeHolder(holder)} 占着，请它让位…`);
    }
    if (Date.now() >= deadline) throw new ProfileBusyError(holder, Date.now() - started);
    sleepSync(pollMs);
  }
}

export function release(home, info) {
  if (!home) return false;
  const current = readLock(home);
  if (!current) return false;
  if (info && info.pid && Number(current.pid) !== Number(info.pid)) return false;
  if (Number(current.pid) !== process.pid) return false;
  try {
    fs.unlinkSync(lockPath(home));
    return true;
  } catch {
    return false;
  }
}
