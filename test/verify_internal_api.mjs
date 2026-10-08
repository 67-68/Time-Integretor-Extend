// 验证：能否在页面里读到 Firebase token，并用它调内部 API。
// 只读（GET），不写任何东西。
import { createRequire } from "module";
import os from "os";
import path from "path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const USER_DATA = process.env.PROFILE_DIR || path.join(os.homedir(), ".focusmate-mcp", "browser-data");

const ctx = await chromium.launchPersistentContext(USER_DATA, { headless: true });
const page = ctx.pages()[0] || (await ctx.newPage());
await page.goto("https://app.focusmate.com/dashboard", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(9000);

// 1) 读 IndexedDB 里的 firebase auth
const token = await page.evaluate(async () => {
  const dbs = await indexedDB.databases();
  const pick = dbs.map((d) => d.name).filter((n) => /firebase/i.test(n));
  const out = { dbs: dbs.map((d) => d.name), pick };
  for (const name of pick) {
    try {
      const db = await new Promise((res, rej) => { const r = indexedDB.open(name); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
      const stores = [...db.objectStoreNames];
      out[name] = stores;
      for (const st of stores) {
        const all = await new Promise((res, rej) => { const tx = db.transaction(st, "readonly"); const q = tx.objectStore(st).getAll(); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
        out[st + "_keys"] = all.map((v) => Object.keys(v || {}));
        out[st + "_raw"] = JSON.stringify(all).slice(0, 600);
      }
    } catch (e) { out[name + "_err"] = String(e); }
  }
  return out;
});
console.log("=== IndexedDB ===");
console.log(JSON.stringify(token, null, 1));

// 2) 从 firebaseLocalStorage 里取 accessToken
const at = await page.evaluate(async () => {
  const db = await new Promise((res, rej) => { const r = indexedDB.open("firebaseLocalStorageDb"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const all = await new Promise((res, rej) => { const tx = db.transaction("firebaseLocalStorage", "readonly"); const q = tx.objectStore("firebaseLocalStorage").getAll(); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
  const rec = all.map((r) => r?.value?.stsTokenManager?.accessToken).filter(Boolean);
  return { count: all.length, tokenPrefix: rec[0] ? rec[0].slice(0, 25) : null, tokenLen: rec[0]?.length ?? 0, token: rec[0] || null };
});
console.log("=== TOKEN ===");
console.log(JSON.stringify({ count: at.count, tokenPrefix: at.tokenPrefix, tokenLen: at.tokenLen }));

if (at.token) {
  const results = await page.evaluate(async (tok) => {
    const out = [];
    const tryIt = async (label, headers) => {
      try {
        const r = await fetch("https://api.focusmate.com/v1/session/next", { headers });
        const t = await r.text();
        out.push({ label, status: r.status, body: t.slice(0, 180) });
      } catch (e) { out.push({ label, error: String(e).slice(0, 120) }); }
    };
    await tryIt("raw", { Authorization: tok });
    await tryIt("bearer", { Authorization: "Bearer " + tok });
    await tryIt("anon", {});
    return out;
  }, at.token);
  console.log("=== API AUTH TEST (GET /v1/session/next) ===");
  for (const r of results) console.log(JSON.stringify(r));
}

// 3) 公开 API 的 sessionId vs dashboard 链接里的 id
const links = await page.evaluate(() => [...document.querySelectorAll("a[href*='/session/']")].map((a) => a.getAttribute("href")));
console.log("=== dashboard session links ===", JSON.stringify(links.slice(0, 6)));

await ctx.close();
process.exit(0);
