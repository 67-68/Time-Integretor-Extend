// scripts/profile_lock.mjs 的回归测试（纯文件 + 子进程，不需要浏览器/网络）。
//   node --test test/profile_lock.test.mjs
//
// 钉住的是「同一个 Chromium profile 只许一个进程开浏览器」这条互斥约定：
// 它失效时的症状不是报错，而是 dashboard 白屏 + IndexedDB 打坏（2026-11-16
// 真机实测：登录态天天腐烂，每天都要重新登录）。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import * as LOCK from "../scripts/profile_lock.mjs";

const SCRIPT = path.resolve("scripts/profile_lock.mjs");
const DEAD_PID = 999999;

function tmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "fmlock-"));
}

/** 在子进程里持锁，等它打出 held 再返回句柄。 */
function holdInChild(home, role = "child-holder") {
  const code = `
    import(${JSON.stringify(SCRIPT)}).then(async (m) => {
      m.acquire(process.argv[1], ${JSON.stringify(role)}, { waitMs: 3000, quiet: true });
      console.log("held");
      setTimeout(() => process.exit(0), 3000);
    });
  `;
  const child = spawn(process.execPath, ["-e", code, home], { stdio: ["ignore", "pipe", "pipe"] });
  return new Promise((resolve, reject) => {
    let buf = "";
    child.stdout.on("data", (d) => {
      buf += d;
      if (buf.includes("held")) resolve(child);
    });
    child.on("exit", (code) => {
      if (!buf.includes("held")) reject(new Error(`child exited ${code}: ${buf}`));
    });
    setTimeout(() => reject(new Error("child 迟迟没拿到锁")), 10000);
  });
}

test("拿锁写文件、放锁删文件", () => {
  const home = tmpHome();
  const info = LOCK.acquire(home, "mcp:book_session", { waitMs: 500, quiet: true });
  assert.equal(info.role, "mcp:book_session");
  assert.equal(info.pid, process.pid);
  assert.equal(LOCK.readLock(home).pid, process.pid);
  assert.ok(LOCK.describeHolder(LOCK.readLock(home)).includes("mcp:book_session"));
  assert.equal(LOCK.release(home, info), true);
  assert.equal(LOCK.readLock(home), null);
  assert.equal(LOCK.release(home, info), false);
});

test("同一进程重复 acquire 会等到超时（互斥真的生效）并留下 want", () => {
  const home = tmpHome();
  LOCK.acquire(home, "first", { waitMs: 500, quiet: true });
  assert.throws(
    () => LOCK.acquire(home, "second", { waitMs: 300, quiet: true }),
    (e) => e instanceof LOCK.ProfileBusyError && e.holder.role === "first"
  );
  assert.ok(LOCK.wantPending(home), "等锁的人应该写 want 标记");
  LOCK.release(home);
  LOCK.release(home);
});

test("死进程的锁会被抢走", () => {
  const home = tmpHome();
  fs.writeFileSync(
    LOCK.lockPath(home),
    JSON.stringify({ pid: DEAD_PID, role: "ghost", ts: Date.now() / 1000, host: os.hostname() })
  );
  assert.equal(LOCK.holderIsStale(LOCK.readLock(home)), true);
  const t0 = Date.now();
  const info = LOCK.acquire(home, "taker", { waitMs: 2000, quiet: true });
  assert.equal(info.role, "taker");
  assert.ok(Date.now() - t0 < 1000, "抢死锁不该白等满超时");
  LOCK.release(home, info);
});

test("ts 太老的锁也算 stale（进程活着也不行）", () => {
  const home = tmpHome();
  fs.writeFileSync(
    LOCK.lockPath(home),
    JSON.stringify({ pid: process.pid, role: "old", ts: Date.now() / 1000 - 3600, host: os.hostname() })
  );
  assert.equal(LOCK.holderIsStale(LOCK.readLock(home)), true);
  LOCK.acquire(home, "taker2", { waitMs: 500, quiet: true });
  assert.equal(LOCK.readLock(home).role, "taker2");
  LOCK.release(home);
});

test("want 标记有 TTL", () => {
  const home = tmpHome();
  fs.writeFileSync(
    LOCK.wantPath(home),
    JSON.stringify({ pid: process.pid, role: "waiter", ts: Date.now() / 1000 })
  );
  assert.equal(LOCK.wantPending(home).role, "waiter");
  fs.writeFileSync(
    LOCK.wantPath(home),
    JSON.stringify({ pid: process.pid, role: "old", ts: Date.now() / 1000 - 3600 })
  );
  assert.equal(LOCK.wantPending(home), null);
  LOCK.clearWant(home);
  assert.equal(LOCK.wantPending(home), null);
});

test("别的进程持锁时抢不到，并且写了 want 请它让位", async () => {
  const home = tmpHome();
  const child = await holdInChild(home);
  try {
    assert.throws(
      () => LOCK.acquire(home, "racer", { waitMs: 600, quiet: true }),
      (e) => e instanceof LOCK.ProfileBusyError && e.holder.role === "child-holder"
    );
    assert.equal(LOCK.wantPending(home).role, "racer");
  } finally {
    child.kill();
  }
});

test("孤儿浏览器探测：假 profile 目录不误报", () => {
  const home = tmpHome();
  assert.deepEqual(LOCK.findProfileBrowsers(path.join(home, "nope")), []);
  assert.deepEqual(LOCK.killProfileBrowsers(path.join(home, "nope"), { quiet: true }), []);
});

test("没给 home 时退化成不上锁（CLI 不能被弄死）", () => {
  assert.equal(LOCK.acquire("", "cli", { waitMs: 10, quiet: true }), null);
  assert.equal(LOCK.release("", null), false);
});
