#!/usr/bin/env python3
"""集成测试（真机）：把 fixtures 喂给真实的 warden，**真的**在 Focusmate 上订两场。

它验的是单元测试永远验不到的那一段：

    plan 分资源 → 切 session → 写 layout/data.csv → keep_session 下单
    → 真实 Focusmate session → 把任务挂成 Focusmate task（attachments）
    → 再对账一遍（不许把刚排好的计划判成失效）

配额与代价（刻意压到最小）
--------------------------
* **每次运行只订 2 场**（75 + 50 分钟）、跑完自动取消，账号回到零。
* 目标日固定 **+10 天**：Focusmate 上肯定还没订，提前取消不算迟到取消。
* 同一台机器 3 天内只允许真跑 1 次（`test/live/.live-ledger.json`）。
* 想不花配额就先跑干跑：`python3 -B test/live/test_live_plan.py`。

跑法
----
    python3 -B test/live/test_live_focusmate.py            # 只打印计划，不订
    python3 -B test/live/test_live_focusmate.py --yes       # 真订（跑完取消）
    python3 -B test/live/test_live_focusmate.py --yes --keep # 真订 + 保留（不取消）

沙盒：`test/live/fixture.py`（3 个 pro / 3 个 SOP op / 目标日一个 13:00–15:30
的时段 → 容量 5 count → 排布 [75, 50]）。除了 `FOCUSMATE_CONFIG_DIR`（真实的
浏览器 profile，不指它就没登录态），其它所有读写都在 /tmp 沙盒里。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixture  # noqa: E402

LEDGER = os.path.join(HERE, ".live-ledger.json")
COOLDOWN_DAYS = 3

fails = []
ok_count = 0


class _SkipLive(Exception):
    """plumbing 模式跑完「不带网络」的那一半，直接跳到收尾。"""


def check(label, got, want):
    global ok_count
    if got == want:
        ok_count += 1
        print("ok   %s" % label)
    else:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
        print("FAIL %s\n      got  = %r\n      want = %r" % (label, got, want))


def check_true(label, got):
    check(label, bool(got), True)


# ---------------------------------------------------------------- 闸门

def load_ledger():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ledger(data):
    data["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def cooldown_left():
    last = load_ledger().get("last_ok_at")
    if not last:
        return None
    try:
        at = datetime.datetime.fromisoformat(last)
    except ValueError:
        return None
    left = datetime.timedelta(days=COOLDOWN_DAYS) - (datetime.datetime.now() - at)
    return left if left.total_seconds() > 0 else None


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def daemon_note():
    path = os.path.join(ROOT, "temp", "keep_session.pid")
    try:
        raw = json.load(open(path, encoding="utf-8"))
        pid = raw.get("pid") if isinstance(raw, dict) else raw
    except (OSError, ValueError):
        return ""
    if pid and pid_alive(pid):
        return ("daemon 正在跑（pid %s）：它和本测试共用同一个浏览器 profile，"
                "两边会为了锁互相等（不会白屏，但可能各多等几十秒）。"
                % pid)
    return ""


def profile_lock_note():
    home = os.path.join(ROOT, "temp", "focusmate-home")
    try:
        info = json.load(open(os.path.join(home, ".profile-lock.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if info and pid_alive(info.get("pid")):
        return ("profile 现在被 %s(pid %s) 占着；它会看到 want 标记让位，"
                "本测试最多多等一个让位周期。" % (info.get("role") or "?", info.get("pid")))
    return ""


# ---------------------------------------------------------------- 跑 warden

def run_warden(env, commands, timeout=900):
    """真跑 `warden.py --exec`：契约视图、脚本模式（不会被琐事屏吞命令）。"""
    argv = [sys.executable, "-B", os.path.join(ROOT, "warden.py"),
            "--exec", "; ".join(commands)]
    t0 = time.time()
    proc = subprocess.run(argv, env=env, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout)
    return proc.returncode, proc.stdout or "", time.time() - t0


def probe(env):
    """独立探针：只问 Focusmate「现在到底有什么」。"""
    proc = subprocess.run([os.environ.get("NODE") or "node",
                           os.path.join(HERE, "probe_live.mjs")],
                          env=env, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=300)
    text = (proc.stdout or "").strip()
    start = text.find("{")
    if start < 0:
        raise RuntimeError("探针没吐 JSON：\n%s" % text[-2000:])
    return json.loads(text[start:])


def read_csv_rows(path):
    """沙盒 data.csv → [(day_iso, start, duration)]。不 import schedule，
    否则会拿到「按环境变量解析过的模块常量」，在测试里反而不好判断。"""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3:
                    continue
                try:
                    out.append((parts[0], parts[1], int(parts[2])))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def plan_items(paths, day):
    """projects.json 里这一天所有 op 的 plan items 条数。"""
    store = json.load(open(paths["projects"], encoding="utf-8"))
    total = 0
    for op in store.get("projects") or []:
        plan = (op.get("plans") or {}).get(day.isoformat()) or {}
        total += len(plan.get("items") or [])
    return total


def local_dt(day, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(h, m))


def meetings_on(data, day):
    out = []
    for m in data.get("meetings") or []:
        try:
            at = datetime.datetime.fromtimestamp(int(m["startMs"]) / 1000.0)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if at.date() == day:
            out.append((at, m))
    out.sort(key=lambda pair: pair[0])
    return out


def tasks_attached_to(data, meeting_id):
    out = []
    for t in data.get("tasks") or []:
        for a in t.get("attachments") or []:
            if str(a.get("externalId") or "") == str(meeting_id):
                out.append(t)
                break
    return out


# ---------------------------------------------------------------- 主流程

def main(argv):
    yes = "--yes" in argv
    keep = "--keep" in argv
    force = "--force" in argv
    offset = fixture.DEFAULT_OFFSET
    for i, tok in enumerate(argv):
        if tok.startswith("--offset="):
            offset = int(tok.split("=", 1)[1])
        elif tok == "--offset" and i + 1 < len(argv):
            offset = int(argv[i + 1])

    day = fixture.target_date(offset)
    slots = [("13:00", 75), ("14:30", 50)]
    print("目标日 %s（+%d 天）：预计订 %s" %
          (day.isoformat(), offset, "、".join("%s(%d)" % s for s in slots)))
    print("会真订 %d 场，%s。" % (len(slots), "跑完取消" if not keep else "**保留不取消**"))

    for note in (daemon_note(), profile_lock_note()):
        if note:
            print("· " + note)

    left = cooldown_left()
    if left and not force:
        print("✋ 冷却中：%s 之后才能再真跑（上次成功在 %s）。"
              % ((datetime.datetime.now() + left).strftime("%m-%d %H:%M"),
                 load_ledger().get("last_ok_at")))
        print("   要硬跑就加 --force。")
        return 2
    if not yes:
        print("\n没有 --yes，什么都不做。")
        return 0

    plumbing = "--plumbing" in argv
    paths = fixture.make_sandbox(offset=offset)
    # plumbing 模式：执行区还是 24 小时，目标日在区外 → `check` 一场都不订。
    # 用来先把「沙盒 → 子进程 → plan → data.csv → 本地任务」这条线跑通，
    # 不花任何 Focusmate 配额。真跑才把窗口放宽到能盖住目标日。
    env = fixture.env_for(paths, live=True,
                          window_hours=24 if plumbing else 24 * (offset + 2))
    print("\n沙盒：%s" % paths["root"])
    ledger = load_ledger()
    ledger["last_start_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    ledger["target_day"] = day.isoformat()
    ledger["sandbox"] = paths["root"]
    save_ledger(ledger)

    cancelled = False
    try:
        # ---- 1) plan（真跑，末尾会自动下单）---------------------------------
        cmds = ["plan %d" % offset]
        cmds += ["task %d %d" % (idx, count) for idx, count in fixture.ALLOCATION]
        cmds.append("complete")
        rc, out, secs = run_warden(env, cmds)
        print("\n── warden --exec %s（%.1fs，exit=%d）" % ("; ".join(cmds), secs, rc))
        print(out[-4000:])
        check("plan 没报错（exit 0）", rc, 0)
        check_true("plan 没有 ❌", "❌" not in out)

        rows = [(start, int(dur)) for day_iso, start, dur in read_csv_rows(paths["csv"])
                if day_iso == day.isoformat()]
        check("data.csv 写的就是这两场", rows, slots)
        tasks = json.load(open(paths["tasks"], encoding="utf-8"))["tasks"]
        check("本地建了 3 条项目任务", len([t for t in tasks
                                          if (t.get("source") or {}).get("kind") == "project"]), 3)

        # ---- 2) 真看到 Focusmate 上的场次 -----------------------------------
        remote = probe(env)
        if plumbing:
            check("plumbing：Focusmate 上这一场都没订（没花配额）",
                  len(meetings_on(remote, day)), 0)
            print("\n（plumbing：跳过下单 / 挂任务 / 对账那几步）")
            raise _SkipLive()
        # Focusmate 的 API 复核会滞后（实测 17:23 那轮：13:00「报成功」但 3 次复核
        # 都没出现，另一场一次就过）。这是外部抖动，不是产品 bug —— force-check
        # 重试有限几轮，把「订上了但 API 还没认」吸收掉。
        for _i in range(3):
            remote = probe(env)
            print("   探针：这一天 %d 场%s" % (
                len(meetings_on(remote, day)),
                "" if "error" not in remote else "（探针报错：%s）" % remote.get("error")))
            if len(meetings_on(remote, day)) >= 2:
                break
            rc, out, secs = run_warden(env, ["day %d" % offset, "force-check"],
                                       timeout=600)
            print("   （复核只看到 %d 场，force-check 重试一轮：%.1fs）"
                  % (len(meetings_on(remote, day)), secs))
            time.sleep(5)
        remote = probe(env)
        check_true("探针读到了 sessions", "meetings" in remote)
        got = meetings_on(remote, day)
        check("Focusmate 上这一天恰好 2 场", len(got), 2)
        check("场次时间对得上（本地时间）",
              [at.strftime("%H:%M") for at, _ in got], [s[0] for s in slots])
        check("场次时长对得上", [int(m.get("duration") or 0) for _, m in got],
              [s[1] for s in slots])

        # ---- 3) 任务真的挂上去了（attachments 指向这一场）---------------------
        # **必须带 `day N`**：每个 --exec 都是新进程，默认看今天；不带的话
        # `refresh --check` 对的是今天，目标日 +10 的 task 永远挂不上去。
        rc, out, secs = run_warden(env, ["day %d" % offset, "refresh --check"])
        print("\n── warden --exec refresh --check（%.1fs，exit=%d）" % (secs, rc))
        print(out[-3000:])
        check("attach 这一轮没报错", rc, 0)

        # 挂载也可能滞后一轮：没挂满就再对账一次（最多两轮）。
        # 3 条任务 / 2 场：10 分钟那条小任务会**叠进**大场，所以允许某一场挂 2 条，
        # 但不能有场次空着，也不能丢任务。
        def snapshot_attached():
            data = probe(env)
            got_ = meetings_on(data, day)
            rows = []
            for at, meeting in got_:
                mine = [t for t in tasks_attached_to(data, meeting.get("id"))
                        if fixture.TITLE_MARK in (t.get("title") or "")]
                rows.append((at.strftime("%H:%M"), len(mine)))
            return rows

        attached = snapshot_attached()
        for _ in range(2):
            if sum(n for _s, n in attached) >= 3:
                break
            run_warden(env, ["day %d" % offset, "refresh --check"], timeout=600)
            time.sleep(4)
            attached = snapshot_attached()
        check("两场都挂上了测试 task（小任务允许叠场）",
              all(n >= 1 for _s, n in attached) and len(attached) == 2, True)
        check("两场加起来正好 3 条", sum(n for _s, n in attached), 3)

        local = json.load(open(paths["tasks"], encoding="utf-8"))["tasks"]
        with_fm = [t for t in local
                   if (t.get("source") or {}).get("kind") == "project"
                   and (t.get("focusmate") or {}).get("task_id")]
        check("本地也记下了 Focusmate task_id", len(with_fm), 3)
        check_true("本地记的 session id 是真 session id",
                   all(len(str((t.get("session") or {}).get("focusmate_session_id") or "")) > 8
                       for t in with_fm))

        # ---- 4) 对账：刚排好的计划不许被判失效 -------------------------------
        rc, out, secs = run_warden(env, ["day %d" % offset, "refresh"])
        print("\n── warden --exec refresh（%.1fs，exit=%d）" % (secs, rc))
        check("对账之后 plan 还在（没被误判清空）", plan_items(paths, day), 3)
        check_true("对账没报「绑定和 plan 不一致」", "绑定和 plan 不一致" not in out)

    except _SkipLive:
        pass
    except Exception as e:                        # noqa: BLE001 - 出错也要把现场留下
        import traceback
        fails.append("异常：%s: %s" % (type(e).__name__, e))
        print("\n‼ 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()
    finally:
        # ---- 5) 清场：取消这一天的场次，把账号还原 --------------------------
        if not keep and not plumbing:
            # 清场要看**实际订上的**，不能因为前面某条断言红了就不收拾烂摊子。
            # 倒序取消：先撤后一场，再撤前一场 —— 无论 warden 是把「已放弃」的
            # 行留着还是删掉，前面的行号都不会被顶掉。
            try:
                rc, out, secs = run_warden(env, ["day %d" % offset,
                                                 "cancel 2", "cancel 1"], timeout=600)
                print("\n── 清场 warden --exec cancel（%.1fs，exit=%d）" % (secs, rc))
                print(out[-2000:])
                remote = probe(env)
                left = meetings_on(remote, day)
                cancelled = not left
                if fails:
                    if left:
                        print("⚠ 清场后还有 %d 场残留（前面已经红了，手动取消）："
                              % len(left))
                else:
                    check("清场后 Focusmate 上这一天 0 场", len(left), 0)
            except Exception as e:                # noqa: BLE001
                print("\n‼ 清场失败：%s: %s" % (type(e).__name__, e))
                fails.append("清场失败：%s" % e)

        ledger = load_ledger()
        ok = not fails
        if ok and not keep and not plumbing:
            # plumbing 没花配额、也没订场，不该吃 3 天冷却
            ledger["last_ok_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        save_ledger(ledger)
        if keep and not fails:
            print("\n· 按 --keep 保留了这两场：%s %s"
                  % (day.isoformat(), "、".join("%s(%d)" % s for s in slots)))
        if not ok:
            print("\n沙盒留在 %s（里面有 data.csv / state / 日志）" % paths["root"])
        elif not keep:
            shutil.rmtree(paths["root"], ignore_errors=True)

    print()
    if fails:
        print("FAILED %d 项：" % len(fails))
        for item in fails:
            print(" - %s" % item)
        if not cancelled and not keep:
            print("\n⚠ 可能还有残留场次，手动去 Focusmate 取消：%s %s"
                  % (day.isoformat(), "、".join("%s(%d)" % s for s in slots)))
        return 1
    print("全绿（%d 项）。" % ok_count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
