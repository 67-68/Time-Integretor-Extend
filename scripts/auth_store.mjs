/**
 * auth_store.mjs —— Focusmate 登录态（Firebase `firebase:authUser`）的快照与自愈。
 *
 * 为什么需要（2026-11-23 真机）
 * ---------------------------
 * cookie 不是登录态：真正让 app 认人的是 IndexedDB
 * `firebaseLocalStorageDb/firebaseLocalStorage` 里那条
 * `firebase:authUser:<apiKey>:[DEFAULT]`，它带着 refresh token。
 * 2026-11-22 13:30 之后这条记录被删掉（cookie 还在、`/dashboard` 被 app 自己踢回
 * `/login`），从此每条 pull / book 都「未登录」，只能人工重登。
 *
 * 所以这里做两件事：
 *   1. 每次拿到有效 token / 登录成功后把那条记录备份成
 *      `<home>/auth-backup.json`（600 权限、原子写入、内容没变不重写）；
 *   2. 下次打开发现跳到 /login 时，把备份写回 IndexedDB 再 reload，让 Firebase
 *      自己用 refresh token 续期 —— refresh token 还有效就**不用人工重登**。
 *
 * 备份里有 refresh token，等同凭据：文件必须 600，不要提交、不要贴日志。
 */
import fs from "node:fs";
import path from "node:path";

export const BACKUP_NAME = "auth-backup.json";
export const IDB_NAME = "firebaseLocalStorageDb";
export const STORE_NAME = "firebaseLocalStorage";

export function authBackupPath(home) {
  return path.join(path.resolve(home), BACKUP_NAME);
}

/** 把 IndexedDB 记录概括成日志/CLI 能直接打印的形状（不含 token 本体）。 */
export function authSummary(rows) {
  const list = Array.isArray(rows) ? rows.filter(Boolean) : [];
  const first = list.find((r) => r && r.value && r.value.stsTokenManager);
  const sts = (first && first.value && first.value.stsTokenManager) || {};
  const tok = typeof sts.accessToken === "string" ? sts.accessToken : "";
  let exp = 0;
  try {
    exp = JSON.parse(Buffer.from(tok.split(".")[1], "base64").toString("utf8")).exp * 1000;
  } catch {
    /* 不是 JWT 就当没有 */
  }
  return {
    count: list.length,
    key: (first && first.fbase_key) || null,
    uid: (first && first.value && first.value.uid) || null,
    email: (first && first.value && first.value.email) || null,
    hasRefresh: !!sts.refreshToken,
    expMs: exp || null,
    exp: exp ? new Date(exp).toISOString() : null,
    tokenLen: tok.length,
  };
}

/** 读备份；没有/坏了/空记录都返回 null（调用方据此决定「要不要提示重登」）。 */
export function loadAuthBackup(home) {
  if (!home) return null;
  try {
    const d = JSON.parse(fs.readFileSync(authBackupPath(home), "utf8"));
    if (!d || !Array.isArray(d.rows) || !d.rows.length) return null;
    return d;
  } catch {
    return null;
  }
}

/**
 * 写备份。内容没变不重写（每条命令都写盘没意义，还会把 mtime 刷花；
 * 600 权限 + rename 原子替换，写一半断电也不会留下半个 JSON）。
 */
export function saveAuthBackup(home, rows, { force = false, source = "auto" } = {}) {
  const list = Array.isArray(rows) ? rows.filter(Boolean) : [];
  if (!home) return { saved: false, reason: "no-home" };
  if (!list.length) return { saved: false, reason: "empty" };
  const p = authBackupPath(home);
  if (!force) {
    const prev = loadAuthBackup(home);
    if (prev && JSON.stringify(prev.rows) === JSON.stringify(list)) {
      return { saved: false, reason: "unchanged", path: p };
    }
  }
  const payload = { savedAt: new Date().toISOString(), source, rows: list };
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = `${p}.tmp-${process.pid}`;
  try {
    fs.writeFileSync(tmp, JSON.stringify(payload), { mode: 0o600 });
    fs.renameSync(tmp, p);
    try {
      fs.chmodSync(p, 0o600);
    } catch {
      /* 权限失败不至于把登录态弄丢 */
    }
    return { saved: true, reason: "written", path: p };
  } catch (e) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* ignore */
    }
    return { saved: false, reason: "write-failed", error: String(e && e.message ? e.message : e) };
  }
}

// ---------------------------------------------------------------- 页面侧

/** 页面上下文：把 firebaseLocalStorage 的所有记录读出来（原始结构，含 refresh token）。 */
export async function dumpAuthRows(page) {
  return page.evaluate(
    async ({ dbName, store }) => {
      try {
        const db = await new Promise((ok, no) => {
          const q = indexedDB.open(dbName);
          q.onsuccess = () => ok(q.result);
          q.onerror = () => no(q.error);
        });
        return await new Promise((ok, no) => {
          const tx = db.transaction(store, "readonly");
          const q = tx.objectStore(store).getAll();
          q.onsuccess = () => ok(q.result || []);
          q.onerror = () => no(q.error);
        });
      } catch (e) {
        return [];
      }
    },
    { dbName: IDB_NAME, store: STORE_NAME }
  );
}

/**
 * 页面上下文：把备份记录写回 IndexedDB。
 *
 * store 是「记录自带 fbase_key、keyPath 就是 fbase_key」的结构（2026-11-23 实测
 * `getAll()` 返回的就是 {fbase_key, value}），所以整条 put 回去，别拆成
 * `put(value, key)` —— 那会 DataError。
 */
export async function restoreAuthRows(page, rows) {
  const list = Array.isArray(rows) ? rows.filter((r) => r && r.fbase_key) : [];
  if (!list.length) return 0;
  return page.evaluate(
    async ({ dbName, store, rows }) => {
      const db = await new Promise((ok, no) => {
        const q = indexedDB.open(dbName);
        q.onsuccess = () => ok(q.result);
        q.onerror = () => no(q.error);
      });
      await new Promise((ok, no) => {
        const tx = db.transaction(store, "readwrite");
        const os = tx.objectStore(store);
        for (const r of rows) os.put(r);
        tx.oncomplete = () => ok();
        tx.onerror = () => no(tx.error);
        tx.onabort = () => no(tx.error);
      });
      return rows.length;
    },
    { dbName: IDB_NAME, store: STORE_NAME, rows: list }
  );
}

/** 打开后备份一次（调用方已经拿到页面、确认能读 token 时用）。 */
export async function snapshotFromPage(page, home, opts = {}) {
  const rows = await dumpAuthRows(page);
  const res = saveAuthBackup(home, rows, opts);
  return { ...res, summary: authSummary(rows) };
}

/** 用备份把页面救回来；返回 {restored, reason, savedAt}。 */
export async function restoreFromBackup(page, home) {
  const backup = loadAuthBackup(home);
  if (!backup) return { restored: 0, reason: "no-backup", savedAt: null };
  let n = 0;
  try {
    n = await restoreAuthRows(page, backup.rows);
  } catch (e) {
    return { restored: 0, reason: "write-failed", error: String(e && e.message ? e.message : e), savedAt: backup.savedAt };
  }
  return { restored: n, reason: n ? "restored" : "empty-backup", savedAt: backup.savedAt };
}

/**
 * 主动续期：直接拿 refresh token 调 Firebase 的 token 接口，把新 id_token /
 * expirationTime 写回 IndexedDB（顺带刷新备份）。
 *
 * 为什么不靠页面 reload：2026-11-23 实测，token 还剩 30 分钟时 reload 三次
 * 也不会刷新（Firebase SDK 只在快过期/被调用时才刷）。等它自己刷，就正好
 * 撞在「refresh 失败 → SDK 把记录删掉 → 只能人工重登」那条路上。
 *
 * 返回 {ok, token, summary, saved} 或 {ok:false, reason, status, detail}。
 */
export async function refreshAuthToken(page, home) {
  const rows = await dumpAuthRows(page);
  const rec = rows.find((r) => r && r.value && r.value.stsTokenManager && r.value.stsTokenManager.refreshToken);
  if (!rec) return { ok: false, reason: "no-refresh-token" };
  const apiKey = String(rec.fbase_key || "").split(":")[2] || "";
  if (!apiKey) return { ok: false, reason: "no-api-key" };
  const body =
    "grant_type=refresh_token&refresh_token=" +
    encodeURIComponent(rec.value.stsTokenManager.refreshToken);
  const out = await page.evaluate(
    async ({ apiKey, body }) => {
      try {
        const r = await fetch(`https://securetoken.googleapis.com/v1/token?key=${apiKey}`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body,
        });
        const json = await r.json().catch(() => ({}));
        return { status: r.status, json };
      } catch (e) {
        return { status: 0, error: String(e) };
      }
    },
    { apiKey, body }
  );
  const detail = out.json && (out.json.error_description || out.json.error || out.json.message);
  if (out.status !== 200 || !out.json || !out.json.id_token) {
    return { ok: false, reason: "http", status: out.status, detail: detail || out.error || null };
  }
  const sts = { ...rec.value.stsTokenManager };
  sts.accessToken = out.json.id_token;
  if (out.json.refresh_token) sts.refreshToken = out.json.refresh_token;
  sts.expirationTime = Date.now() + (Number(out.json.expires_in) || 3600) * 1000;
  const updated = { ...rec, value: { ...rec.value, stsTokenManager: sts } };
  const rest = rows.filter((r) => r !== rec);
  await restoreAuthRows(page, [updated]);
  const saved = saveAuthBackup(home, [updated, ...rest], { source: "refresh" });
  return { ok: true, token: out.json.id_token, summary: authSummary([updated]), saved };
}

/** 测试/救援用：清掉这个 profile 里的登录记录（不动 cookie）。 */
export async function wipeAuthRows(page) {
  return page.evaluate(
    async ({ dbName, store }) => {
      const db = await new Promise((ok, no) => {
        const q = indexedDB.open(dbName);
        q.onsuccess = () => ok(q.result);
        q.onerror = () => no(q.error);
      });
      // clear() 的 result 是 undefined，所以先数一遍再清（CLI 要能报「清了几条」）
      const before = await new Promise((ok, no) => {
        const q = db.transaction(store, "readonly").objectStore(store).count();
        q.onsuccess = () => ok(q.result || 0);
        q.onerror = () => no(q.error);
      });
      await new Promise((ok, no) => {
        const tx = db.transaction(store, "readwrite");
        const q = tx.objectStore(store).clear();
        q.onsuccess = () => ok();
        q.onerror = () => no(q.error);
        tx.oncomplete = () => ok();
        tx.onerror = () => no(tx.error);
        tx.onabort = () => no(tx.error);
      });
      return before;
    },
    { dbName: IDB_NAME, store: STORE_NAME }
  );
}

// ---------------------------------------------------------------- CLI

/**
 * 用法（FOCUSMATE_CONFIG_DIR 决定用哪个 home，和 bridge/daemon 一致）：
 *   node scripts/auth_store.mjs --check      # 开一次浏览器，校验登录态并刷新备份
 *   node scripts/auth_store.mjs --snapshot   # 同上（--check 的别名，语义更直白）
 *   node scripts/auth_store.mjs --wipe       # 清掉 IDB 登录记录（测试/排查用）
 *   node scripts/auth_store.mjs --recover    # 只用备份救一次，不依赖已有 token
 *   node scripts/auth_store.mjs --refresh    # 立刻用 refresh token 续期一次并写回
 *
 * 输出一行 `RESULT_JSON {...}`，方便 main.py / live 测试解析。
 * 退出码：0 成功；2 未登录且救不回来；3 环境/浏览器起不来。
 */
async function main() {
  const argv = process.argv.slice(2);
  const mode = argv.find((a) => a.startsWith("--")) || "--check";
  // _lib 在模块加载时就解析 profile，所以必须先写 env 再 import。
  const lib = await import("./_lib.mjs");
  const AUTH = await import("./auth_store.mjs");
  const emit = (obj) => console.log("RESULT_JSON " + JSON.stringify(obj));

  let ctx;
  let page;
  let restored = null;
  try {
    if (mode === "--wipe") {
      const s = await lib.openSession({ headless: true, role: "auth-wipe", softAuth: false });
      ctx = s.ctx;
      page = s.page;
      const n = await AUTH.wipeAuthRows(page);
      emit({ ok: true, wiped: n, profile: lib.describeProfile() });
      return 0;
    }
    let s;
    try {
      s = await lib.openSession({ headless: true, role: "auth-check", softAuth: mode !== "--wipe" });
    } catch (e) {
      if (e && e.authSoft) {
        // 真因（含「有没有备份」那句）必须打给看日志的人，别只留在 RESULT_JSON 里
        console.error(e.message);
        emit({ ok: false, reason: "logged-out", restored: e.restore || null, profile: lib.describeProfile() });
        return 2;
      }
      console.error("[auth_store][ERROR]", e && e.message ? e.message : e);
      emit({ ok: false, reason: "env-error", error: String(e && e.message ? e.message : e) });
      return 3;
    }
    ctx = s.ctx;
    page = s.page;
    if (mode === "--recover") {
      restored = await AUTH.restoreFromBackup(page, lib.CONFIG_DIR);
      if (restored.restored) {
        await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
        await page.waitForTimeout(6000);
      }
    }
    if (mode === "--refresh") {
      const rr = await AUTH.refreshAuthToken(page, lib.CONFIG_DIR);
      emit({ ok: !!rr.ok, summary: rr.summary || null, saved: rr.saved || null, reason: rr.reason || null, status: rr.status || null, detail: rr.detail || null, profile: lib.describeProfile() });
      return rr.ok ? 0 : 2;
    }
    const loggedIn = !page.url().includes("/login");
    if (!loggedIn) {
      emit({ ok: false, reason: "logged-out", restored, profile: lib.describeProfile() });
      return 2;
    }
    const rows = await AUTH.dumpAuthRows(page);
    const summary = AUTH.authSummary(rows);
    if (!summary.count) {
      emit({ ok: false, reason: "no-auth-record", summary, profile: lib.describeProfile() });
      return 2;
    }
    const saved = AUTH.saveAuthBackup(lib.CONFIG_DIR, rows, { source: "auth_store:" + (mode === "--recover" ? "recover" : "check") });
    emit({ ok: true, summary, saved, restored, backup: AUTH.authBackupPath(lib.CONFIG_DIR), profile: lib.describeProfile() });
    return 0;
  } finally {
    if (ctx) await ctx.close().catch(() => {});
  }
}

const invokedDirectly = (() => {
  try {
    return path.resolve(process.argv[1] || "") === path.resolve(new URL(import.meta.url).pathname);
  } catch {
    return false;
  }
})();

if (invokedDirectly) {
  main()
    .then((code) => process.exit(code))
    .catch((e) => {
      console.error("[auth_store][ERROR]", e && e.message ? e.message : e);
      process.exit(3);
    });
}
