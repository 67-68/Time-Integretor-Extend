// scripts/mcp_home.mjs 的回归测试（纯函数，不需要 Playwright / 浏览器 / 网络）。
//   node --test test/mcp_home.test.mjs
//
// 钉住的是「bridge 到底读哪个 profile」——这条错了一次，代价是整套 TUI 每条
// 命令都「未登录」，而日志/界面只给一句「内部调用了 process.exit(1)」。
import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import { resolveProfile, hasProfile, expandUser, WORKSPACE_HOME, LEGACY_HOME }
  from "../scripts/mcp_home.mjs";

const WS = "/repo/temp/focusmate-home";
const LEGACY = "/home/me/.focusmate-mcp";

/** 假装这几个目录存在（只认 `browser-data/Default` 这个判据）。 */
function existsIn(...homes) {
  const set = new Set(homes.map((h) => path.join(h, "browser-data", "Default")));
  return (p) => set.has(p);
}
const nothing = () => false;

function resolve({ env = {}, exists = nothing } = {}) {
  return resolveProfile({ env, exists, workspace: WS, legacy: LEGACY });
}

test("FOCUSMATE_PROFILE_DIR 点名了就用它（调试/换账号的路子）", () => {
  const r = resolve({ env: { FOCUSMATE_PROFILE_DIR: "/tmp/p" }, exists: existsIn(WS) });
  assert.equal(r.profileDir, "/tmp/p");
  assert.equal(r.source, "env:profile");
});

test("FOCUSMATE_CONFIG_DIR 定运行目录，profile 是它下面的 browser-data", () => {
  const r = resolve({ env: { FOCUSMATE_CONFIG_DIR: "/data/home" }, exists: existsIn(WS) });
  assert.equal(r.home, "/data/home");
  assert.equal(r.profileDir, "/data/home/browser-data");
  assert.equal(r.source, "env:home");
});

test("FOCUSMATE_MCP_HOME 是同一个东西的旧名字", () => {
  const r = resolve({ env: { FOCUSMATE_MCP_HOME: "/data/home" } });
  assert.equal(r.profileDir, "/data/home/browser-data");
});

test("两个都点名时，profile 压过运行目录", () => {
  const r = resolve({
    env: { FOCUSMATE_CONFIG_DIR: "/data/home", FOCUSMATE_PROFILE_DIR: "/tmp/p" },
  });
  assert.equal(r.profileDir, "/tmp/p");
  assert.equal(r.home, "/data/home");
});

test("没点名：工作区的 profile 在就用工作区（和 main.py 一致）", () => {
  const r = resolve({ exists: existsIn(WS, LEGACY) });
  assert.equal(r.home, WS);
  assert.equal(r.profileDir, path.join(WS, "browser-data"));
  assert.equal(r.source, "workspace");
});

test("工作区没有、老位置有 → 用老位置，绝不凭空造一个新 profile", () => {
  // 这条是本次事故的反面：老位置有登录态的人不该因为升级就被踢去一个空目录。
  const r = resolve({ exists: existsIn(LEGACY) });
  assert.equal(r.home, LEGACY);
  assert.equal(r.source, "legacy");
});

test("两边都没有 → 工作区默认（main.py 首次会建目录并迁移旧登录态）", () => {
  const r = resolve({});
  assert.equal(r.home, WS);
  assert.equal(r.source, "workspace(default)");
});

test("只有空壳 browser-data 不算有 profile", () => {
  // 复制到一半 / 从没登录过的目录长得就像这样，用它 = 造一个没登录的新 profile
  assert.equal(hasProfile(WS, (p) => p === path.join(WS, "browser-data")), false);
  assert.equal(hasProfile(WS, existsIn(WS)), true);
  assert.equal(hasProfile("", existsIn(WS)), false);
});

test("环境变量里的 ~ 会展开", () => {
  assert.equal(expandUser("~/x"), path.join(os.homedir(), "x"));
  assert.equal(expandUser("~"), os.homedir());
  assert.equal(expandUser("  /abs/p  "), "/abs/p");
  assert.equal(expandUser(""), "");
  const r = resolve({ env: { FOCUSMATE_CONFIG_DIR: "~/mcp" } });
  assert.equal(r.home, path.join(os.homedir(), "mcp"));
});

test("真环境里解析出来的 profile 一定带 browser-data，且不是 undefined", () => {
  // 不用替身跑一遍：路径拼接错了（拼出 "undefined/browser-data"）这种事故
  // 只有真跑一次才看得见。
  const r = resolveProfile();
  assert.ok(path.isAbsolute(r.profileDir), r.profileDir);
  assert.ok(r.profileDir.endsWith("browser-data"), r.profileDir);
  assert.ok(!r.profileDir.includes("undefined"), r.profileDir);
  assert.ok(["env:profile", "env:home", "workspace", "legacy", "workspace(default)"]
    .includes(r.source), r.source);
  assert.equal(WORKSPACE_HOME, path.join(path.resolve(WORKSPACE_HOME, "..", ".."),
    "temp", "focusmate-home"));
  assert.equal(LEGACY_HOME, path.join(os.homedir(), ".focusmate-mcp"));
});
