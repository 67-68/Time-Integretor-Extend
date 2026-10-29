#!/usr/bin/env node
/**
 * mcp_home.mjs —— 「Focusmate 运行目录 / 浏览器 profile 在哪」的**唯一**解析点。
 *
 * 存在的理由（真踩过）
 * -------------------
 * main.py 早就会因为「主机上 `~/.focusmate-mcp` 不可写」把运行目录切到工作区的
 * `temp/focusmate-home`（登录态第一次也迁过去了）。但 `_lib.mjs` 里还是那句
 *
 *     process.env.FOCUSMATE_CONFIG_DIR || ~/.focusmate-mcp
 *
 * 而 warden.py 起 bridge 时**没传** FOCUSMATE_CONFIG_DIR（只有显式
 * `--profile-dir` 才传 FOCUSMATE_PROFILE_DIR）。于是同一天里：
 *
 *    daemon  → temp/focusmate-home/browser-data   （登录态好的，一直在用）
 *    bridge  → ~/.focusmate-mcp/browser-data      （9 月的老 profile，早过期）
 *
 * bridge 打开的是那个老 profile，页面进 /login，`_lib` 的 `die()` 调
 * `process.exit`，bridge 把它转成「内部调用了 process.exit(1)，已转成命令级失败」
 * —— 前端只看到这句，半点线索都没有。真正的话（「未登录…」）在
 * temp/warden-bridge.log 里。
 *
 * 所以：**解析只写这一份**，python 侧（main.py / warden.py）和 node 侧（_lib /
 * bridge / 各 CLI 脚本）用同一条优先级，谁也别自己拼路径：
 *
 *   1. FOCUSMATE_PROFILE_DIR     直接点名 profile（最高优先，调试/换账号用）
 *   2. FOCUSMATE_CONFIG_DIR      整个运行目录（main.py 会设它）
 *   3. FOCUSMATE_MCP_HOME        同上（旧名字）
 *   4. 工作区 temp/focusmate-home —— **但只有它真的带着 profile 才用**
 *   5. ~/.focusmate-mcp          同上（老位置，都还在用老位置的人不受影响）
 *   6. 谁都没有 → 工作区默认目录（main.py 首次会建 + 把旧登录态搬过去）
 *
 * 4/5 那条「真带着 profile 才用」是有意的：光看目录在不在会选中一个空的
 * `browser-data`，那等于凭空造一个没登录的新 profile，比报错还难查。
 *
 * 纯函数 + 可注入 `exists`/`env`，所以 `node --test test/mcp_home.test.mjs`
 * 能不装 playwright、不开浏览器就把优先级钉住。
 */
import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const WORKSPACE_HOME = path.join(REPO_ROOT, "temp", "focusmate-home");
export const LEGACY_HOME = path.join(os.homedir(), ".focusmate-mcp");

/** `~` / `~foo` 展开（node 的 path 不认，而环境变量里常带）。 */
export function expandUser(p) {
  const s = String(p == null ? "" : p).trim();
  if (!s) return "";
  if (s === "~") return os.homedir();
  if (s.startsWith("~/")) return path.join(os.homedir(), s.slice(2));
  return s;
}

/**
 * 这个目录里有没有**真**的 Chromium profile。
 * 判据只认 `browser-data/Default/`：Chromium 持久化 profile 必定有它，
 * 而只有 `browser-data/` 空壳子的目录是「复制到一半」或「从没登录过」。
 */
export function hasProfile(home, exists = fs.existsSync) {
  if (!home) return false;
  return !!exists(path.join(home, "browser-data", "Default"));
}

/**
 * 解析出 `{ home, profileDir, source }`。
 *
 * source 是**给人看的**，会出现在 bridge 的 ping/where 和报错里 ——
 * 排查「到底读了哪个 profile」时，这一句比十行日志都有用。
 */
export function resolveProfile({
  env = process.env,
  exists = fs.existsSync,
  workspace = WORKSPACE_HOME,
  legacy = LEGACY_HOME,
} = {}) {
  const envProfile = expandUser(env.FOCUSMATE_PROFILE_DIR || "");
  const envHome = expandUser(
    (env.FOCUSMATE_CONFIG_DIR || "").trim() || (env.FOCUSMATE_MCP_HOME || "").trim()
  );

  // 1. 点名了 profile：就以它为准，home 只用来在报错里说清楚上下文
  if (envProfile) {
    const profileDir = path.resolve(envProfile);
    const home = envHome ? path.resolve(envHome) : path.dirname(profileDir);
    return { home, profileDir, source: "env:profile" };
  }
  // 2/3. 点名了运行目录
  if (envHome) {
    const home = path.resolve(envHome);
    return { home, profileDir: path.join(home, "browser-data"), source: "env:home" };
  }
  // 4/5. 没点名：谁的 profile 真在就用谁（工作区优先，和 main.py 一致）
  if (hasProfile(workspace, exists)) {
    return { home: workspace, profileDir: path.join(workspace, "browser-data"), source: "workspace" };
  }
  if (hasProfile(legacy, exists)) {
    return { home: legacy, profileDir: path.join(legacy, "browser-data"), source: "legacy" };
  }
  // 6. 都没有：工作区默认（main.py 首次会建目录 + 迁移旧登录态）
  return {
    home: workspace,
    profileDir: path.join(workspace, "browser-data"),
    source: "workspace(default)",
  };
}
