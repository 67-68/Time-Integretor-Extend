// scripts/auth_store.mjs 的单元测试（纯文件 + 内存版 IndexedDB，不需要浏览器/网络）。
//   node --test test/auth_store.test.mjs
//
// 钉住的是 2026-11-22/23「登录态天天腐烂」那次事故的两条约定：
//   1. 备份文件必须 600、原子替换、内容没变不重写；
//   2. 备份能被写回 firebaseLocalStorage（整条 put，按 fbase_key），
//      空备份 / 没有备份时必须老实返回 0，而不是假装成功。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import * as AUTH from "../scripts/auth_store.mjs";

// ---------------------------------------------------------------- 测试替身

function req() {
  return { result: undefined, onsuccess: null, onerror: null };
}

/** 内存版 IndexedDB：只实现 auth_store 用到的那几个 op（open/transaction/put/getAll/clear）。 */
function installFakeIndexedDB() {
  const data = new Map(); // fbase_key -> 记录
  const db = {
    objectStoreNames: { contains: () => true },
    transaction() {
      const tx = { oncomplete: null, onerror: null, onabort: null };
      tx.objectStore = () => ({
        put(record) {
          data.set(record.fbase_key, record);
          const q = req();
          queueMicrotask(() => q.onsuccess && q.onsuccess());
          return q;
        },
        getAll() {
          const q = req();
          q.result = [...data.values()];
          queueMicrotask(() => q.onsuccess && q.onsuccess());
          return q;
        },
        count() {
          const q = req();
          q.result = data.size;
          queueMicrotask(() => q.onsuccess && q.onsuccess());
          return q;
        },
        clear() {
          data.clear();
          const q = req();
          q.result = undefined;
          queueMicrotask(() => q.onsuccess && q.onsuccess());
          return q;
        },
      });
      queueMicrotask(() => tx.oncomplete && tx.oncomplete());
      return tx;
    },
  };
  globalThis.indexedDB = {
    open() {
      const q = req();
      queueMicrotask(() => {
        q.result = db;
        q.onsuccess && q.onsuccess();
      });
      return q;
    },
  };
  return { data, clear: () => data.clear() };
}

/** 假 page：evaluate 直接在 node 里跑传进来的函数（配合上面的假 indexedDB）。 */
const fakePage = { evaluate: async (fn, arg) => fn(arg) };

function tmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "fmauth-"));
}

function fakeRecord(key, { token = null, refresh = null } = {}) {
  const sts = {};
  if (token) sts.accessToken = token;
  if (refresh) sts.refreshToken = refresh;
  return {
    fbase_key: key,
    value: { uid: "u-1", email: "a@b.c", stsTokenManager: sts },
  };
}

/** 造一个 exp 很远的假 JWT（不是真签名，只为让 authSummary 解析 exp）。 */
function fakeJwt(expSeconds) {
  const payload = Buffer.from(JSON.stringify({ exp: expSeconds })).toString("base64");
  return `h.${payload}.s`;
}

// ---------------------------------------------------------------- 备份文件

test("saveAuthBackup 写入 600 文件、无临时文件、可 load 回来", () => {
  const home = tmpHome();
  const rows = [fakeRecord("firebase:authUser:k:[DEFAULT]", { token: fakeJwt(4102444800), refresh: "r" })];
  const res = AUTH.saveAuthBackup(home, rows, { force: true, source: "test" });
  assert.equal(res.saved, true);
  assert.equal(res.reason, "written");
  const p = AUTH.authBackupPath(home);
  assert.ok(fs.existsSync(p));
  assert.equal(fs.statSync(p).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(home).filter((f) => f.includes(".tmp")), []);
  const back = AUTH.loadAuthBackup(home);
  assert.equal(back.rows.length, 1);
  assert.equal(back.rows[0].fbase_key, rows[0].fbase_key);
  assert.equal(back.source, "test");
});

test("内容没变不重写（mtime 不变）", () => {
  const home = tmpHome();
  const rows = [fakeRecord("firebase:authUser:k:[DEFAULT]", { token: fakeJwt(4102444800), refresh: "r" })];
  AUTH.saveAuthBackup(home, rows, { force: true });
  const p = AUTH.authBackupPath(home);
  const before = fs.statSync(p).mtimeMs;
  const res = AUTH.saveAuthBackup(home, rows);
  assert.equal(res.saved, false);
  assert.equal(res.reason, "unchanged");
  assert.equal(fs.statSync(p).mtimeMs, before);
});

test("内容变了会重写；空 rows 不写", () => {
  const home = tmpHome();
  AUTH.saveAuthBackup(home, [fakeRecord("k1", { token: fakeJwt(4102444800) })], { force: true });
  const res = AUTH.saveAuthBackup(home, [fakeRecord("k2", { token: fakeJwt(4102444800) })]);
  assert.equal(res.saved, true);
  assert.equal(AUTH.loadAuthBackup(home).rows[0].fbase_key, "k2");

  const home2 = tmpHome();
  assert.deepEqual(
    (() => {
      const r = AUTH.saveAuthBackup(home2, []);
      return { saved: r.saved, reason: r.reason };
    })(),
    { saved: false, reason: "empty" }
  );
  assert.equal(fs.existsSync(AUTH.authBackupPath(home2)), false);
});

test("备份坏了/不存在 → loadAuthBackup 返回 null", () => {
  const home = tmpHome();
  assert.equal(AUTH.loadAuthBackup(home), null);
  fs.writeFileSync(AUTH.authBackupPath(home), "{not json");
  assert.equal(AUTH.loadAuthBackup(home), null);
  fs.writeFileSync(AUTH.authBackupPath(home), JSON.stringify({ rows: [] }));
  assert.equal(AUTH.loadAuthBackup(home), null);
});

// ---------------------------------------------------------------- 概括

test("authSummary 区分无记录 / 有 refresh / 只有 access token", () => {
  assert.deepEqual(
    (() => {
      const s = AUTH.authSummary([]);
      return { count: s.count, hasRefresh: s.hasRefresh, exp: s.exp };
    })(),
    { count: 0, hasRefresh: false, exp: null }
  );
  const expSec = 4102444800;
  const full = AUTH.authSummary([fakeRecord("k", { token: fakeJwt(expSec), refresh: "r" })]);
  assert.equal(full.count, 1);
  assert.equal(full.hasRefresh, true);
  assert.equal(full.expMs, expSec * 1000);
  assert.equal(full.exp, new Date(expSec * 1000).toISOString());
  const noRefresh = AUTH.authSummary([fakeRecord("k", { token: fakeJwt(expSec) })]);
  assert.equal(noRefresh.hasRefresh, false);
});

// ---------------------------------------------------------------- 页面侧读写

test("dumpAuthRows / restoreAuthRows / wipeAuthRows 走假 IndexedDB", async () => {
  const { data, clear } = installFakeIndexedDB();
  clear();
  const rows = [
    fakeRecord("firebase:authUser:k:[DEFAULT]", { token: fakeJwt(4102444800), refresh: "r1" }),
    fakeRecord("firebase:authUser:k2:[DEFAULT]", { token: fakeJwt(4102444800), refresh: "r2" }),
  ];
  const n = await AUTH.restoreAuthRows(fakePage, rows);
  assert.equal(n, 2);
  assert.deepEqual([...data.keys()].sort(), rows.map((r) => r.fbase_key).sort());

  const dumped = await AUTH.dumpAuthRows(fakePage);
  assert.equal(dumped.length, 2);
  assert.ok(dumped.every((r) => r.fbase_key && r.value));

  await AUTH.wipeAuthRows(fakePage);
  assert.equal(data.size, 0);
  assert.deepEqual(await AUTH.dumpAuthRows(fakePage), []);
});

test("restoreAuthRows 空输入不写、返回 0；restoreFromBackup 没有备份时明说 no-backup", async () => {
  const { data } = installFakeIndexedDB();
  data.clear();
  assert.equal(await AUTH.restoreAuthRows(fakePage, []), 0);
  assert.equal(data.size, 0);

  const home = tmpHome();
  const res = await AUTH.restoreFromBackup(fakePage, home);
  assert.equal(res.restored, 0);
  assert.equal(res.reason, "no-backup");
});

test("restoreFromBackup 把备份写回并返回条数与时间", async () => {
  const { data } = installFakeIndexedDB();
  data.clear();
  const home = tmpHome();
  const rows = [fakeRecord("firebase:authUser:k:[DEFAULT]", { token: fakeJwt(4102444800), refresh: "r" })];
  AUTH.saveAuthBackup(home, rows, { force: true });
  const res = await AUTH.restoreFromBackup(fakePage, home);
  assert.equal(res.restored, 1);
  assert.equal(res.reason, "restored");
  assert.ok(res.savedAt);
  assert.equal(data.size, 1);

  // 再救一次会发现记录已在（restore 仍然幂等写回，不报错）
  const again = await AUTH.restoreFromBackup(fakePage, home);
  assert.equal(again.restored, 1);
});

// ---------------------------------------------------------------- 主动续期

/** 临时替换全局 fetch，跑完恢复。 */
async function withFetch(mock, fn) {
  const orig = globalThis.fetch;
  globalThis.fetch = mock;
  try {
    return await fn();
  } finally {
    globalThis.fetch = orig;
  }
}

test("refreshAuthToken：成功时写回新 token 并刷新备份", async () => {
  const { data } = installFakeIndexedDB();
  data.clear();
  const home = tmpHome();
  const oldExp = 4102444800;
  const rows = [fakeRecord("firebase:authUser:KEY123:[DEFAULT]", { token: fakeJwt(oldExp), refresh: "r-old" })];
  AUTH.saveAuthBackup(home, rows, { force: true });
  await AUTH.restoreAuthRows(fakePage, rows);

  const newJwt = fakeJwt(oldExp + 3600);
  const res = await withFetch(
    async (url, opts) => {
      assert.ok(url.includes("securetoken.googleapis.com"));
      assert.ok(String(opts.body).includes("grant_type=refresh_token"));
      assert.ok(String(opts.body).includes("r-old"));
      return { status: 200, json: async () => ({ id_token: newJwt, expires_in: 3600, refresh_token: "r-new" }) };
    },
    () => AUTH.refreshAuthToken(fakePage, home)
  );
  assert.equal(res.ok, true);
  assert.equal(res.token, newJwt);
  assert.equal(res.summary.expMs, (oldExp + 3600) * 1000);
  // IndexedDB 里的记录被更新
  const dumped = await AUTH.dumpAuthRows(fakePage);
  assert.equal(dumped[0].value.stsTokenManager.accessToken, newJwt);
  assert.equal(dumped[0].value.stsTokenManager.refreshToken, "r-new");
  // 备份也更新了
  assert.equal(AUTH.loadAuthBackup(home).rows[0].value.stsTokenManager.accessToken, newJwt);
});

test("refreshAuthToken：服务器拒绝时返回 http 状态与原因", async () => {
  const { data } = installFakeIndexedDB();
  data.clear();
  const home = tmpHome();
  const rows = [fakeRecord("firebase:authUser:KEY123:[DEFAULT]", { token: fakeJwt(4102444800), refresh: "r" })];
  AUTH.saveAuthBackup(home, rows, { force: true });
  await AUTH.restoreAuthRows(fakePage, rows);
  const res = await withFetch(
    async () => ({ status: 400, json: async () => ({ error: "invalid_grant", error_description: "Token has been revoked." }) }),
    () => AUTH.refreshAuthToken(fakePage, home)
  );
  assert.equal(res.ok, false);
  assert.equal(res.reason, "http");
  assert.equal(res.status, 400);
  assert.match(res.detail, /revoked/);
});

test("refreshAuthToken：记录里没有 refresh token 时明说", async () => {
  const { data } = installFakeIndexedDB();
  data.clear();
  const rows = [fakeRecord("firebase:authUser:KEY123:[DEFAULT]", { token: fakeJwt(4102444800) })]; // 没有 refresh
  await AUTH.restoreAuthRows(fakePage, rows);
  const res = await AUTH.refreshAuthToken(fakePage, tmpHome());
  assert.equal(res.ok, false);
  assert.equal(res.reason, "no-refresh-token");
});
