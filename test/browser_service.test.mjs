// scripts/bridge.mjs --serve 的回归测试（纯 unix socket + 子进程，不需要浏览器/网络）。
//   node --test test/browser_service.test.mjs
//
// 钉住的是「浏览器只归一个常驻服务所有」这条新架构的四条硬性质：
//   ①同一 sock 只能有一个服务（第二次必须退出，不能悄悄起第二只浏览器）；
//   ②多客户端共用一条命令队列，响应必须回给发命令的那个客户端（不串台）；
//   ③客户端断开不能杀服务（否则 warden 一退，daemon 就没浏览器了）；
//   ④没人连着的时候要记下 idleSince（到位后关浏览器），有人在连就别关。
//
// 真正的「到点关浏览器」需要真 Chromium，属于 live 测试（test/live/）。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const SCRIPT = path.resolve("scripts/bridge.mjs");

function mkHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "browsersvc-"));
}

function envFor(home, extra = {}) {
  return {
    ...process.env,
    FOCUSMATE_CONFIG_DIR: home,
    TI_LOG_DIR: path.join(home, "logs"),
    WARDEN_BRIDGE_IDLE_MS: "3000",
    ...extra,
  };
}

/** 起一个服务，等 socket 能连上（最多 10s）。 */
async function startService(home, extra = {}) {
  const sock = path.join(home, "browser-service.sock");
  const child = spawn(process.execPath, [SCRIPT, "--serve", sock], {
    env: envFor(home, extra),
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (d) => {
    stderr += d;
  });
  child.stdout.resume();
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (fs.existsSync(sock)) {
      const alive = await new Promise((resolve) => {
        const s = net.connect(sock);
        s.once("connect", () => {
          s.destroy();
          resolve(true);
        });
        s.once("error", () => resolve(false));
      });
      if (alive) return { child, sock, stderr: () => stderr };
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  child.kill("SIGKILL");
  throw new Error("服务没起来：" + stderr.slice(-500));
}

/** 一个最小客户端：发一条命令收一条响应，事件单独收。 */
function makeClient(sock) {
  const s = net.connect(sock);
  s.setEncoding("utf8");
  let buf = "";
  const waiters = new Map();
  const events = [];
  s.on("data", (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      const f = JSON.parse(line);
      if (f.event) {
        events.push(f);
        continue;
      }
      const w = waiters.get(f.id);
      if (w) {
        waiters.delete(f.id);
        w(f);
      }
    }
  });
  s.on("error", () => {});
  return {
    events,
    send(id, cmd, kw = {}) {
      return new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error("等不到 " + id + " 的响应")), 5000);
        waiters.set(id, (f) => {
          clearTimeout(t);
          resolve(f);
        });
        s.write(JSON.stringify({ id, cmd, ...kw }) + "\n");
      });
    },
    close() {
      s.destroy();
    },
    raw: s,
  };
}

function waitGone(child, ms = 5000) {
  return new Promise((resolve) => {
    if (child.exitCode !== null) return resolve({ code: child.exitCode, signal: child.signalCode });
    const t = setTimeout(() => resolve({ code: null, signal: "timeout" }), ms);
    child.once("exit", (code, signal) => {
      clearTimeout(t);
      resolve({ code, signal });
    });
  });
}

test("同一 sock 只能起一个服务（第二次退出码 3）", async () => {
  const home = mkHome();
  const a = await startService(home);
  try {
    const b = spawn(process.execPath, [SCRIPT, "--serve", a.sock], {
      env: envFor(home),
      stdio: ["ignore", "pipe", "pipe"],
    });
    let err = "";
    b.stderr.setEncoding("utf8");
    b.stderr.on("data", (d) => {
      err += d;
    });
    b.stdout.resume();
    const got = await waitGone(b);
    assert.equal(got.code, 3, "第二次必须非 0 退出，实际 " + JSON.stringify(got));
    assert.match(err, /已经有一个服务在跑/, "要说清楚是单实例，别让人以为是别的问题");
  } finally {
    a.child.kill("SIGKILL");
  }
});

test("多客户端共用队列：响应回给发命令的那一方，不串台", async () => {
  const home = mkHome();
  const svc = await startService(home);
  const x = makeClient(svc.sock);
  const y = makeClient(svc.sock);
  try {
    const pings = [];
    for (let i = 1; i <= 3; i++) {
      pings.push(x.send("x-" + i, "ping"));
      pings.push(y.send("y-" + i, "ping"));
    }
    const all = await Promise.all(pings);
    assert.equal(all.length, 6);
    // 每个响应都得带回它自己的请求号（id 撞车 = 调用方永远等不到）
    assert.deepEqual(all.map((f) => f.id).sort(), ["x-1", "x-2", "x-3", "y-1", "y-2", "y-3"]);
    for (const f of all) {
      assert.equal(f.ok, true);
      assert.equal(f.cmd, "ping");
      assert.equal(typeof f.result.pid, "number");
      assert.equal(f.result.serve, true);
    }
    // 同一个服务进程（不是各起一只）
    const pids = new Set(all.map((f) => f.result.pid));
    assert.equal(pids.size, 1);
  } finally {
    x.close();
    y.close();
    svc.child.kill("SIGKILL");
  }
});

test("门牌文件里带 CDP 端口（客户端靠它去 connectOverCDP）", async () => {
  const home = mkHome();
  const svc = await startService(home);
  try {
    const state = JSON.parse(fs.readFileSync(path.join(home, "browser-service.json"), "utf8"));
    assert.equal(state.sock, svc.sock);
    assert.equal(typeof state.cdpPort, "number");
    assert.ok(state.cdpPort > 0, "应该分到一个真实端口，实际 " + state.cdpPort);
  } finally {
    svc.child.kill("SIGKILL");
  }
});

test("客户端断开不杀服务，别的客户端还能接着用", async () => {
  const home = mkHome();
  const svc = await startService(home);
  const a = makeClient(svc.sock);
  const first = await a.send(1, "ping");
  a.close();
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(svc.child.exitCode, null, "一个客户端走了，服务不该跟着退");
  const b = makeClient(svc.sock);
  try {
    const second = await b.send(2, "ping");
    assert.equal(second.result.pid, first.result.pid, "必须是同一个服务进程");
  } finally {
    b.close();
    svc.child.kill("SIGKILL");
  }
});

test("没有客户端连着时记 idleSince；有人连着时不排空闲关浏览器", async () => {
  const home = mkHome();
  const svc = await startService(home, { WARDEN_BRIDGE_IDLE_MS: "60000" });
  const a = makeClient(svc.sock);
  try {
    const connected = await a.send(1, "ping");
    assert.equal(connected.result.clientsNow, 1);
    assert.equal(connected.result.idleArmed, false, "有客户端连着就不该排空闲关闭");
    // startService 的探活连接也会留下一个 idleSince，所以这里比的是「时间有没有往后推」
    const sinceBefore = connected.result.idleSince;
    a.close();
    await new Promise((r) => setTimeout(r, 300));
    const b = makeClient(svc.sock);
    try {
      const after = await b.send(2, "ping");
      assert.equal(typeof after.result.idleSince, "number", "最后一个客户端走了要记下时间");
      assert.ok(after.result.idleSince > sinceBefore, "idleSince 应该被刷新到这次离开的时刻");
      assert.equal(after.result.clientsNow, 1);
      assert.equal(after.result.idleMs, 60000);
    } finally {
      b.close();
    }
  } finally {
    svc.child.kill("SIGKILL");
  }
});

test("上次被 kill -9 留下的 sock 不会挡住重启", async () => {
  const home = mkHome();
  const first = await startService(home);
  const before = await (async () => {
    const c = makeClient(first.sock);
    const r = await c.send(1, "ping");
    c.close();
    return r.result.pid;
  })();
  first.child.kill("SIGKILL");
  await waitGone(first.child);
  assert.equal(fs.existsSync(first.sock), true, "kill -9 会留下 sock 文件，这正是要覆盖的场景");
  const second = await startService(home);
  try {
    const c = makeClient(second.sock);
    const r = await c.send(1, "ping");
    c.close();
    assert.notEqual(r.result.pid, before, "重启后应该是新进程");
  } finally {
    second.child.kill("SIGKILL");
  }
});
