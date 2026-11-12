"""warden_core.task_cmd_mixin —— task 命令解析、后缀分流、CRUD、剪贴板导入与搜索/排序/分组/翻页。"""
import copy
import datetime
import re

from .common import (
    IMPORT_PENDING,
    ImportBatch,
    NET_PENDING,
    SRC_OMNIFOCUS,
    TASK_HOOK_FLAGS,
    TASK_HOOK_LABELS,
    TASK_MARK_STATES,
    _task_bool_word,
    cfg,
    normalize_task_column,
    of,
    pj,
    rl,
    task_table_headers,
    th,
    ts,
)
from .views import TaskView


class TaskCmdMixin(object):
    # -- 任务系统 ------------------------------------------------------

    def cmd_task(self, args):
        """TaskView 的唯一入口：task <create|delete|modify|assign|select|repeat|sync|archive> ...

        裸任务 ID 就是 select（``task 8`` ≡ ``task select 8``），所以最短的挂法是
        ``task 8 -a 1``。

        任何子命令后面都可以跟这些后缀（都能组合）：
          ``--assign <session ID>``（``-a`` 同义）= 顺手挂到那一场
          ``--note <备注>``（``-n`` 同义）= 顺手写备注（可以塞网址）
          ``--mark complete|discard``（``-m`` 同义）= 手动改本地状态
          ``--from <session ID>``（``-s`` 同义）= 批量：那一场上的**全部**任务
          ``--trigger <预设> [参数...]``（``-t`` 同义）= 挂创建触发（框架；默认空）
          ``--formatter <预设> [参数...]``（``-f`` 同义）= 挂排期算法并当场执行
          ``--assign-count <N>``（``-act`` 同义）= 覆盖**这一场**要记的 count

        ``-act`` 只能作用于已经挂到 session 上的任务；它可以和 ``-ac`` / ``-a``
        组合，先挂载再改本场 count：
            task 5 -ac -act 2     ← 挂到当前场，并把本场 count 设为 2
            task 5 -act 1         ← 已经挂着时，只把本场 count 改成 1

        ``-m/--mark`` 是**账本动作**，不代替 ``done`` / ``drop`` 的外部联动：
          - ``complete`` = 本地 completed + 归档；不勾 Focusmate，不当场写 OmniFocus
          - ``discard``  = 本地 dropped；完全不动 Focusmate / OmniFocus
        之后的 ``task sync`` / ``refresh --check`` 仍会按单向契约把本地 completed
        写回 OmniFocus；discard 没有自动写回。

        ``-a`` 有两个特殊取值，替代「表格编号」：
          ``none``    = 下架（删掉 Focusmate 那条 task，本地断绑定）→ ``task 8 -an``
          ``current`` = 当前这一场（进行中的优先，其次最近那一场）→ ``task 8 -ac``

            task 8 -a 1
            task create title=写论文 --assign 3
            task create title=写论文 -n "材料在这 https://example.com/x" --assign 3
            task modify 1 due 后天 --note=下午再弄
            task select 1 -n "https://notion.so/xxx"
            task 8 -an                  ← 从它那一场下架（≡ task 8 -a none）
            task 8 -ac                  ← 挂到当前这一场（≡ task 8 -a current）
            task 3 -m complete          ← 只把本地 #3 标成完成（并归档）
            task -m discard 3           ← 只把本地 #3 标成放弃（外头都不动）
            task -t                    ← 列 trigger 预设（默认是空列表）
            task -f                    ← 列 formatter 预设（现在有 end / enlist / repeat）
            task 3 -f end              ← 按 duration 排到当天最后一段并自动 assign
            task 3 -f repeat 10:45     ← 固定挂到起点 10:45 的已有 session
            task 3 -e true             ← 这条独占一整场（同一场不能再放第二条任务）
            task create title=写论文 duration=25 -e true -f end
            task create title=闪卡 duration=25 -f end
            task 32 swap 21             ← 两条任务互换位置
            task -s 5 -a 6              ← 把 #5 上的全部任务转移到 #6
            task -s 5 -a none           ← 把 #5 上的全部任务下架
            task -s 5                   ← 只列 #5 上现在挂着谁

        「挂」永远是同一套：先在 Focusmate 建 task 并复核，成功后才落本地
        （见 :meth:`cmd_task_assign`），所以 ``--assign`` 不会产生假成功的半截状态。
        """
        toks = [str(a) for a in args]
        if not toks:
            raise ValueError("用法：task create/delete/modify/note/assign/select/repeat/sync/archive ...")
        # 分工固定成一条规矩：**动作归子命令，后缀归分流**。
        #   task import          → 动作：读剪贴板（不需要任何 flag）
        #   task import -a 3     → 后缀：顺手分流/挂到表格 #3
        #   task 5 link          → 动作：读剪贴板里的链接
        # 所以 ``-a`` 永远只表示 ``--assign``（分流），不兼职别的意思 ——
        # 同一个字母两副面孔，是这个入口最容易踩的坑。
        assign_to, kept = self._split_assign(toks)
        source, kept = self._split_source(kept)
        note_text, kept = self._split_note(kept)
        exclusive, kept = self._split_exclusive(kept)
        mark_status, kept = self._split_mark(kept)
        count_spec, kept = self._split_count(kept)
        assign_count_spec, kept = self._split_assign_count(kept)
        trigger_tokens, kept = self._split_hook(kept, "trigger")
        formatter_tokens, kept = self._split_hook(kept, "formatter")

        # `task -t` / `task -f` 后面什么都没跟 = 列预设，不是「挂一个空函数」。
        bare_hooks = [kind for kind, tokens in
                      (("trigger", trigger_tokens), ("formatter", formatter_tokens))
                      if tokens is not None and not tokens]
        if bare_hooks:
            self._list_hooks(bare_hooks)
            return True

        trigger_spec = (th.build_spec("trigger", trigger_tokens)
                        if trigger_tokens else None)
        formatter_spec = (th.build_spec("formatter", formatter_tokens)
                          if formatter_tokens else None)

        if source is not None:
            if mark_status is not None:
                raise ValueError("`-m/--mark` 是单条任务的状态标记，不能和 `-s/--from` 批量一起用；"
                                 "先切到那天再逐条标。")
            if count_spec is not None:
                raise ValueError("`-c/--count` 是单条任务的进度，不能和 `-s/--from` 批量一起用；"
                                 "先切到那天再逐条加。")
            if trigger_spec is not None or formatter_spec is not None:
                raise ValueError("`-t/--trigger`、`-f/--formatter` 都是单条任务的钩子，"
                                 "不能和 `-s/--from` 批量一起用；先切到那天再逐条挂。")
            # `-s` 本身就是动作（批量），后面不该再有子命令：``task -s 5 -a 6``。
            if kept:
                raise ValueError("`-s/--from` 是批量后缀，后面不要再跟动作（%s）；"
                                 "写成 `task -s 5 -a 6` 这样。" % kept[0])
            return self._apply_bulk(source, assign_to)
        if not kept and mark_status is None and trigger_spec is None \
                and formatter_spec is None and exclusive is None \
                and count_spec is None and assign_count_spec is None:
            raise ValueError("task 后面要跟动作（create/delete/modify/assign/select/repeat/sync）"
                             "或后缀 -e/--exclusive / -m/--mark / -c/--count"
                             " / -act/--assign-count / -t/--trigger / -f/--formatter")
        # 异步 action（import 这类要先读外面世界的）拿走**同一份**后缀，
        # 后台那半件事落地之后自己套 —— 和同步路径完全一样的那套 _apply_suffix_chain。
        self._import_suffix_doc = {
            "assign_to": assign_to,
            "note_text": note_text,
            "exclusive": exclusive,
            "mark_status": mark_status,
            "count_spec": count_spec,
            "assign_count_spec": assign_count_spec,
            "trigger_spec": trigger_spec,
            "formatter_spec": formatter_spec,
        }
        if kept:
            action = kept[0].lower()
            rest = kept[1:]
            try:
                result = self._dispatch_task(action, rest)
            finally:
                # 这份后缀只借给**这一次**分派用：直接调 cmd_task_import 的地方
                # （测试、内部调用）不该被上一次命令的后缀污染。
                self._import_suffix_doc = None
        else:
            # `task -m complete` / `task -f end`：作用于当前选中的任务。
            action = "hook" if (trigger_spec or formatter_spec) and mark_status is None else "mark"
            result = None
        if result is NET_PENDING:
            # 已经排进后台队列了：后缀跟着 Action 的参数走，不在这儿套
            return None
        if result is IMPORT_PENDING:
            # 重复任务导入在等 y/n/q，**还没有**建出任务。这时 -a / -n / -m /
            # -t / -f 绝不能落到「上一次选中的任务」上（用户反馈：task import
            # -ac 的 -ac 不生效，反而把旧任务挂到了当前这一场）。先存起来，
            # 等 _answer_pending_import() 真正建完再按同样顺序套上去。
            pending = self.pending_import or {}
            pending["suffix"] = {
                "assign_to": assign_to,
                "note_text": note_text,
                "exclusive": exclusive,
                "mark_status": mark_status,
                "count_spec": count_spec,
                "assign_count_spec": assign_count_spec,
                "trigger_spec": trigger_spec,
                "formatter_spec": formatter_spec,
            }
            self.pending_import = pending
            return None
        if isinstance(result, ImportBatch):
            # 一次导入多条：后缀按条套一遍（`-a 3` = 整批都挂到 #3，
            # `-f end` = 五条各自排进当天最后一段），最后那个当选中项。
            for one in result:
                self._apply_suffix_chain(
                    one, action, note_text=note_text, assign_to=assign_to,
                    exclusive=exclusive, mark_status=mark_status,
                    count_spec=count_spec, assign_count_spec=assign_count_spec,
                    trigger_spec=trigger_spec,
                    formatter_spec=formatter_spec)
            return result.last
        if note_text is not None:
            self._apply_note(result, note_text, action)
        if count_spec is not None:
            result = self._apply_count(result, count_spec, action) or result
        if exclusive is not None:
            result = self._apply_exclusive(result, exclusive, action) or result
        if assign_to is not None:
            self._apply_assign(result, assign_to, action)
        if mark_status is not None:
            result = self._apply_mark(result, mark_status, action)
        if trigger_spec is not None:
            result = self._apply_trigger(result, trigger_spec, action)
        if formatter_spec is not None:
            result = self._apply_formatter(result, formatter_spec, action)
        if assign_count_spec is not None:
            result = self._apply_assign_count(result, assign_count_spec, action) or result
        return result

    @staticmethod
    def _split_assign(toks):
        """把 ``--assign N`` / ``--assign=N`` / ``-a N`` 从参数里摘出来（分流后缀）。"""
        assign_to = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--assign", "-a"):
                if i + 1 >= len(toks):
                    raise ValueError("--assign 后面要跟表格里的 session ID（例如 --assign 3）")
                assign_to = toks[i + 1]
                i += 2
                continue
            if low.startswith("--assign="):
                assign_to = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        return assign_to, kept

    @staticmethod
    def _split_source(toks):
        """把 ``--from N`` / ``--from=N`` / ``-s N`` 从参数里摘出来（批量后缀）。

        ``-s`` 说的是**源 session**：它上面挂着的全部任务一起办事。和 ``-a``
        一起用就是「把这一场上的任务搬到那一场」：

            task -s 5 -a 6      把 #5 的全部任务转到 #6
            task -s 5 -a none   把 #5 的全部任务下架
            task -s 5           只列 #5 上现在挂着谁（什么都不动）
        """
        source = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = str(tok).lower()
            if low in ("--from", "-s"):
                if i + 1 >= len(toks):
                    raise ValueError("--from 后面要跟表格里的 session ID（例如 -s 5）")
                source = toks[i + 1]
                i += 2
                continue
            if low.startswith("--from="):
                source = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        return source, kept

    @staticmethod
    def _split_note(toks):
        """把 ``--note 文本`` / ``-n 文本`` / ``--note=文本`` 从参数里摘出来。

        和 ``--assign`` 一样是「任何 task 子命令都能带」的后缀：
        命令跑完拿到任务之后顺手写备注。
        备注是自由文本，**一路吃到下一个 ``--flag`` 为止**，所以不用加引号：

            task create title=写论文 --note 材料在这 https://example.com/x --assign 3

        ``--note=文本`` / ``--note=``（等号后为空 = 清空）也行；
        备注本身以 ``-`` 开头时用等号写法。
        """
        note = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--note", "-n"):
                # 备注是自由文本：一路吃到下一个 --flag 为止，所以
                # `--note 材料在这 https://x` 不用加引号。
                j = i + 1
                parts = []
                while j < len(toks) and not str(toks[j]).startswith("-"):
                    parts.append(str(toks[j]))
                    j += 1
                if not parts:
                    raise ValueError("--note 后面要跟备注文本（例如 --note https://…）")
                note = " ".join(parts)
                i = j
                continue
            if low.startswith("--note="):
                note = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        return note, kept

    @staticmethod
    def _split_exclusive(toks):
        """把 ``--exclusive true`` / ``-e true`` / ``-e=false`` 摘出来。

        独占 = **这一场 session 只能有它一条任务**。和 ``-a`` / ``-n`` 一样是后缀，
        位置随便：

            task 3 -e true              # 只改这一条的账本字段
            task create title=写论文 duration=25 -e true
            task 3 -f end               # formatter end 排出来的任务**自动**独占
            task 3 -e false             # 取消独占（这一场又可以放第二条了）

        裸写 ``-e``（后面不是 true/false）就是 true：``task 3 -e`` ≡ ``task 3 -e true``。
        """
        value = None
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in ("--exclusive", "-e"):
                nxt = str(toks[i + 1]).strip().lower() if i + 1 < len(toks) else ""
                got = _task_bool_word(nxt)
                if got is None:
                    value = True                 # 裸写 -e：就是「独占」
                    i += 1
                else:
                    value = got
                    i += 2
                continue
            if low.startswith("--exclusive=") or low.startswith("-e="):
                word = tok.split("=", 1)[1].strip().lower()
                got = _task_bool_word(word)
                if got is None:
                    raise ValueError("`-e/--exclusive` 只认 true / false；你给的是 %r"
                                     % tok.split("=", 1)[1])
                value = got
                i += 1
                continue
            kept.append(tok)
            i += 1
        return value, kept

    @staticmethod
    def _split_mark(toks):
        """把 ``--mark complete`` / ``--mark=complete`` / ``-m discard`` 摘出来。

        和 ``-a`` / ``-n`` 一样是后缀，位置随便：``task 3 -m complete`` 和
        ``task -m complete 3`` 等价。``-m`` 只认两个值：

          ``complete`` = 本地 completed + 归档（不勾 Focusmate、不写 OmniFocus）
          ``discard``  = 本地 dropped（完全不动外部）
        """
        status = None
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in ("--mark", "-m"):
                if i + 1 >= len(toks):
                    raise ValueError("--mark 后面要跟 complete 或 discard"
                                     "（例如 task 3 -m complete）")
                value = str(toks[i + 1])
                i += 2
            elif low.startswith("--mark=") or low.startswith("-m="):
                value = tok.split("=", 1)[1]
                i += 1
            else:
                kept.append(tok)
                i += 1
                continue
            want = TASK_MARK_STATES.get(value.strip().lower())
            if want is None:
                raise ValueError("`-m/--mark` 只认 complete 或 discard；你给的是 %r" % value)
            if status is not None and status != want:
                raise ValueError("`-m/--mark` 一次只能给一个状态（complete / discard）。")
            status = want
        return status, kept

    @staticmethod
    def _split_count(toks):
        """把 ``--count +2`` / ``-c 5`` / ``-c=+2`` 摘出来（count 后缀）。

        三种写法（用户需求原话）：

            task 3 -c 5     设定最高 count = 5（duration 跟着 = 125 分钟）
            task 3 -c +2    进度 +2；加完够数 = 直接完成任务（不会超过最高 count）
            task 3 -c -1    进度 -1（最低 0）

        ``+``/``-`` 写在数值里，所以 ``-c -1`` 里的 ``-1`` 是**值**不是 flag ——
        取值时按位置拿下一个 token，不看它长什么样。
        """
        spec = None
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in ("--count", "-c"):
                if i + 1 >= len(toks):
                    raise ValueError("`-c/--count` 后面要跟一个数（5 / +2 / -1）；"
                                     "例如 task 3 -c +2")
                value = str(toks[i + 1]).strip()
                i += 2
            elif low.startswith("--count=") or low.startswith("-c="):
                value = tok.split("=", 1)[1].strip()
                i += 1
            else:
                kept.append(tok)
                i += 1
                continue
            if spec is not None:
                raise ValueError("`-c/--count` 一次只能给一个（要么设定、要么加减）。")
            if not re.match(r"^[+-]?\d+$", value):
                raise ValueError("`-c/--count` 只认整数；`+2` 是加、`-1` 是减、`5` 是设定。"
                                 "你给的是 %r" % value)
            spec = value
        return spec, kept

    @staticmethod
    def _split_assign_count(toks):
        """把 ``--assign-count N`` / ``-act N`` / ``-act=N`` 从参数里摘出来。

        它和 ``-c/--count`` 是两本账：

            -c 5        改这条任务/本地 part 的最高 count（时长跟着变）
            -act 2      只改**当前这一场**要记的 count（任务总量/时长不动）

        ``-act`` 是紧凑写法。取值只能是一个正整数；必须等任务已经挂上 session
        才能落地，所以调用方要把它排在 ``-a`` / ``-ac`` 后面执行。
        """
        spec = None
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in ("--assign-count", "--assigned-count", "-act", "--act"):
                if i + 1 >= len(toks):
                    raise ValueError("`-act/--assign-count` 后面要跟这一场记几个 "
                                     "count（例如 task 5 -ac -act 2）")
                value = str(toks[i + 1]).strip()
                i += 2
            elif (low.startswith("--assign-count=")
                  or low.startswith("--assigned-count=")
                  or low.startswith("-act=")
                  or low.startswith("--act=")):
                value = tok.split("=", 1)[1].strip()
                i += 1
            else:
                kept.append(tok)
                i += 1
                continue
            if spec is not None:
                raise ValueError("`-act/--assign-count` 一次只能给一个值。")
            if not re.match(r"^\d+$", value) or int(value) <= 0:
                raise ValueError("`-act/--assign-count` 只认正整数；"
                                 "你给的是 %r" % value)
            spec = int(value)
        return spec, kept

    def _apply_count(self, task, spec, action="count"):
        """把 ``-c`` 落到任务上：设定最高 count，或者推进/退回已投入的 count。

        「加完正好/超过最高进度 = 等同于完成任务」（用户需求），所以够数的时候
        走的是**和 ``task done`` 完全一样**的那条路：本地完成 + 归档 + 勾 Focusmate
        + 写回 OmniFocus。不在这里另开一套「悄悄标完成」的岔路。
        """
        if spec is None or not isinstance(task, dict) or task.get("id") is None:
            return task
        text = str(spec).strip()
        if text.startswith("+") or text.startswith("-"):
            step = int(text)
            out = self.task_store.add_count(task["id"], step)
            task = out["task"]
            now_text = self._count_progress_text(out)
            if step == 0:
                self.say("任务 %s 的 count 没动（%s）：加 0 什么都不会发生。"
                         % (self.task_label(task), now_text), "info")
                return task
            if out.get("maxed"):
                self.say("🎯 任务 %s 的 count 到位了（%s）—— 等同完成任务，走 done 那一套。"
                         % (self.task_label(task), now_text), "ok")
                return self._finish_task_with_count(task)
            verb = "加" if step > 0 else "减"
            self.say("✅ 任务 %s %s了 %d 个 count：%s%s"
                     % (self.task_label(task), verb, abs(step), now_text,
                        "（已经顶到最高 count，不会超过）" if out.get("capped") else ""),
                     "ok")
            return task
        # 纯数字 = 设定最高 count（= 同时设定时长）
        task, before, after = self.task_store.set_count(task["id"], text)
        if after is None:
            self.say("✅ 任务 %s 的最高 count 已取消（duration 没动）。"
                     % self.task_label(task), "ok")
            return task
        self.say("✅ 任务 %s 的最高 count = %d（= %d 分钟）%s"
                 % (self.task_label(task), after,
                    ts.count_to_minutes(after) or 0,
                    ("，原来是 %d" % before) if before else ""), "ok")
        return task

    def _apply_assign_count(self, result, spec, action="assign-count"):
        """``-act/--assign-count``：只改任务**当前这一场**要记几个 count。

        必须在 ``-a`` / ``-ac`` 之后执行（``task 5 -ac -act 2``）。它写进
        ``session.assigned_count``，不改 ``fields.count`` / ``fields.duration``，
        所以不会把自动排期指纹弄脏、触发一次多余重排。
        """
        task = result if isinstance(result, dict) and result.get("id") is not None else None
        if task is None and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            raise ValueError("`-act/--assign-count` 要跟一条任务："
                             "`task 5 -ac -act 2`，或先 `task select 5`。")
        task = self.task_store.get(task["id"])
        if not ts.TaskStore.is_open(task):
            raise ValueError("任务 %s 已经收工/放弃，不能改这一场的 count。"
                             % self.task_label(task))
        session = task.get("session") or {}
        if not session.get("date") or not session.get("start"):
            raise ValueError("任务 %s 还没挂到 session；先 `task %s -ac`（或 `-a <场>`）"
                             "挂上，再用 `-act N` 设置这一场的 count。"
                             % (self.task_label(task), self.task_label(task)))
        before = ts.task_session_assigned_count(task)
        task = self.task_store.set_session_count(task["id"], int(spec))
        session = task.get("session") or {}
        cap = ts.task_count(task)
        session_cap = ts.minutes_to_count(session.get("duration"))
        notes = []
        if cap is not None and int(spec) > cap:
            notes.append("超过任务这一段的 count 上限 %d，收尾记账会被压住" % cap)
        if session_cap is not None and int(spec) > session_cap:
            notes.append("超过这一场的整场容量 %d 个 count" % session_cap)
        self.say("✅ 任务 %s 在 %s %s 这一场的 count = %d%s%s"
                 % (self.task_label(task), session.get("date"),
                    session.get("start"), int(spec),
                    ("（原来是 %d）" % before) if before is not None else "",
                    ("；注意：" + "；".join(notes)) if notes else ""),
                 "ok" if not notes else "warn")
        return task

    def cmd_task_assign_count(self, args):
        """``task <序号> assign-count <N>``：设置任务当前这一场的 count。

        等价于后缀写法 ``task <序号> -act N``（``--assign-count N``）。任务必须
        已经挂上 session；它不能替代 ``-a`` / ``-ac``，只能改已挂那一场的值。
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <序号> assign-count <正整数>"
                             "（或 task <序号> -act N）")
        if len(toks) > 2:
            raise ValueError("task assign-count 只认「序号 + 一个正整数」")
        if len(toks) == 1:
            task = None
            if self.selected_task_id is not None:
                try:
                    task = self.task_store.get(self.selected_task_id)
                except ValueError:
                    task = None
            if task is None:
                raise ValueError("task assign-count 要跟任务序号：task 5 assign-count 2")
            value = toks[0]
        else:
            task = self.resolve_task(toks[0])
            value = toks[1]
        if not re.match(r"^\d+$", value) or int(value) <= 0:
            raise ValueError("assign-count 只认正整数；你给的是 %r" % value)
        return self._apply_assign_count(task, int(value), "assign-count")

    @staticmethod
    def _count_progress_text(out):
        """``(1+2/5)`` 那种进度的纯文字版：``2/5（+2）``。"""
        cap = out.get("cap")
        before, after = out.get("before") or 0, out.get("after") or 0
        if cap is None:
            return "已投入 %d 个 count（没有上限）" % after
        return "%d/%d（%s%d）" % (after, cap, "+" if after >= before else "",
                                 after - before)

    def _finish_task_with_count(self, task):
        """count 够数 = 完成任务：和 ``task done`` 走同一条路（含外部联动）。"""
        return self._complete_task(task, why="count-done")

    @staticmethod
    def _split_hook(toks, kind):
        """把 ``--formatter end [参数...]`` / ``-f end`` 从参数里摘出来。

        返回 ``(tokens, kept)``：

        * 没出现这个 flag → ``(None, kept)``；
        * 出现了但后面没有词 → ``([], kept)`` —— 调用方据此**列预设**；
        * 出现了且后面有词 → ``(["end", ...], kept)``。

        参数一路吃到下一个 ``-`` 开头的 flag 为止，所以
        ``task 3 -f end -a 2`` 里 ``-a 2`` 仍然归 ``--assign``。
        预设名字/参数不能以 ``-`` 开头（key=value 的参数值可以）。
        """
        flags = TASK_HOOK_FLAGS.get(kind)
        if not flags:
            raise ValueError("未知钩子类型 %r" % (kind,))
        found = False
        parts = []
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in flags or low.startswith(flags[0] + "=") or low.startswith(flags[1] + "="):
                if found:
                    raise ValueError("`%s` 一次只能给一个（%s）；不能同时挂两个 %s。"
                                     % (flags[0], " / ".join(flags), kind))
                found = True
                if "=" in tok:
                    # --formatter=end / -f=end：第一个词在等号右边。
                    first = tok.split("=", 1)[1]
                    if first:
                        parts.append(first)
                    i += 1
                else:
                    i += 1
                while i < len(toks) and not str(toks[i]).startswith("-"):
                    parts.append(str(toks[i]))
                    i += 1
                continue
            kept.append(tok)
            i += 1
        return (parts if found else None), kept

    def _list_hooks(self, kinds):
        """``task -t`` / ``task -f``：把预设函数列出来（trigger 默认是空列表）。"""
        for kind in kinds:
            rows = th.describe_presets(kind)
            label = TASK_HOOK_LABELS.get(kind, kind)
            if not rows:
                self.say("%s：0 个（空列表 —— 框架已就位，还没有注册任何函数）" % label,
                         "info")
                continue
            self.say("%s：%d 个" % (label, len(rows)), "info")
            for name, params, help_text in rows:
                tail = ("（参数：%s）" % "、".join(params)) if params else "（无参数）"
                self.say("   %s %s%s" % (name, tail, (" —— " + help_text) if help_text else ""),
                         "info")

    def _hook_task(self, result, kind, action):
        """给 ``-t`` / ``-f`` 找任务：刚产出的优先，其次当前选中的。"""
        task = result if isinstance(result, dict) and result.get("id") is not None else None
        if task is None and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            raise ValueError("`-%s/--%s` 要跟一条任务：`task 3 -%s ...`，"
                             "或先 `task select 3`。"
                             % (kind[0], kind, kind[0]))
        return task

    def _apply_trigger(self, result, spec, action):
        """``-t/--trigger`` 的收尾：把 preset 记在任务上（**不执行**）。

        trigger 的执行属于未来的 runner；这里只负责「挂上去、看得见、可校验」。
        """
        if action in self.NO_TASK_ACTIONS:
            self.say("`-t/--trigger` 对 %s 没意义（它不产出任务），忽略。" % action, "warn")
            return None
        task = self._hook_task(result, "trigger", action)
        task = self.task_store.set_hook(task["id"], "trigger", spec)
        params = " ".join(spec.get("params") or ())
        self.say("✅ 任务 %s「%s」已挂 trigger %s%s（只记本地；runner 还没实现）"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    spec.get("name"), (" " + params) if params else ""), "ok")
        self.selected_task_id = task["id"]
        return task
    NO_TASK_ACTIONS = ("delete", "sync", "refresh", "reconcile", "archive", "history",
                       "归档", "list", "ls", "show", "done", "complete", "完成",
                       "drop", "放弃")

    def _apply_note(self, result, text, action):
        """``--note`` 的收尾：给刚被创建/修改/选中的那条任务写备注。"""
        if action in self.NO_TASK_ACTIONS:
            self.say("`--note` 对 %s 没意义（它不产出任务，没有能写备注的对象），忽略。"
                     % action, "warn")
            return None
        task_id = None
        if isinstance(result, dict) and result.get("id") is not None:
            task_id = result["id"]
        elif self.selected_task_id is not None:
            task_id = self.selected_task_id
        if task_id is None:
            self.say("`--note` 没找到要写备注的任务：先 task create / task select。", "warn")
            return None
        try:
            task = self.task_store.set_notes(task_id, text)
        except ValueError:
            self.say("`--note`：任务 %s 已经不在了（%s 之后没得写），忽略。"
                     % (task_id, action), "warn")
            return None
        self.say("✅ 任务 %s 备注：%s"
                 % (self.task_label(task), self.task_store.notes(task) or "（空）"), "ok")
        return task

    def _apply_exclusive(self, result, value, action):
        """``-e/--exclusive`` 的收尾：把「这一场只能有它一条任务」记进账本。

        独占只落在 ``fields.exclusive``（任务库不校验字段），真正起作用的是两处：

        * **formatter 排期**：独占任务的占用扩展成**整场** session，同一场剩下的
          时间不会再被下一条任务排进去（见 :func:`task_hooks.collect_reservations`）；
        * **refresh --check**：发现一场里挤了两条（其中有独占的）→ 全部下架 + 警告。

        ``end`` 排出来的任务**自动**独占，所以这里多半是手动给导入 / 手工任务补上。
        """
        if action in self.NO_TASK_ACTIONS:
            self.say("`-e/--exclusive` 对 %s 没意义（它不产出任务），忽略。" % action, "warn")
            return None
        task = result if isinstance(result, dict) and result.get("id") is not None else None
        if task is None and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            raise ValueError("`-e/--exclusive` 要跟一条任务：`task 3 -e true` / "
                             "`task create title=写论文 -e true`")
        want = bool(value)
        task = self.task_store.modify(task["id"], "exclusive", want)
        if want:
            self.say("🔒 任务 %s 设为独占：这一场 session 只能有它一条任务。"
                     % self.task_label(task), "ok")
        else:
            self.say("🔓 任务 %s 取消独占：这一场又能并别的任务了。"
                     % self.task_label(task), "ok")
        return task

    def _apply_assign(self, result, session_token, action):
        """``--assign`` 的收尾：拿到任务就挂，拿不到就说一声（不改任何东西）。

        ``result`` 是**任务对象**（create/duplicate/select 的返回值），所以这里直接
        走内部的 :meth:`_assign_task` —— 绝不能再把它扔回 ``cmd_task_assign`` 让
        「序号解析」重新认一遍（内部 id 和面板序号是两套数，绕回去就会挂错任务）。
        """
        # 只认**本地任务**（有 fields 的那种）。项目记录也带 id，但它不是任务：
        # `task import <项目链接> -a 1` 不该顺手把「上一次选中的任务」挂上去。
        task = (result if isinstance(result, dict)
                and result.get("id") is not None
                and isinstance(result.get("fields"), dict) else None)
        if task is None and action != "import" and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            self.say("`--assign` 只对会产出任务的动作用得上（create/modify/select）；"
                     "%s 没有可挂的任务，忽略。" % action, "warn")
            return None
        self.selected_task_id = task["id"]
        return self._assign_task(task, str(session_token))

    def _apply_mark(self, result, status, action):
        """``-m/--mark`` 的收尾：只改本地账本，不碰 Focusmate / OmniFocus。

        和 ``done`` / ``drop`` 的分工：
          * ``done`` = 本地完成 + 归档 + 勾 Focusmate + 写回 OmniFocus
          * ``drop`` = 本地放弃（默认还让 OmniFocus 放弃）
          * ``-m complete`` / ``-m discard`` = **只动账本**。

        本地 completed 之后，下一次 ``task sync`` / ``refresh --check`` 仍会按
        单向契约把完成写回 OmniFocus；discard 则没有任何自动外部动作。
        """
        task = result if isinstance(result, dict) and result.get("id") is not None else None
        if task is None and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            raise ValueError("`-m/--mark` 要跟一条任务：`task 3 -m complete` / "
                             "`task -m discard 3`（没有选中的任务可标）")
        task_id = task["id"]
        if status == ts.STATUS_COMPLETED and ts.TaskStore.is_repeat_template(task):
            raise ValueError("任务 %s 是重复任务**本体**，不能标记完成；完成的是它生成的"
                             "子任务。要结束这套重复：`task repeat %s off` 或 "
                             "`task %s drop`（手动放弃）。"
                             % (self.task_label(task), self.task_label(task),
                                self.task_label(task)))
        current = self.task_store.status_of(task)
        if current == status:
            self.say("任务 %s 已经是「%s」，没有改。"
                     % (self.task_label(task),
                        "完成" if status == ts.STATUS_COMPLETED else "放弃"), "info")
            return task
        if status == ts.STATUS_COMPLETED:
            task = self.task_store.complete(task_id, reason="cmd-mark-complete")
            self.task_store.archive_local(self.task_store.get(task_id),
                                          reason="cmd-mark-complete")
            note_sub = self._project_child_completed(task, writeback=False)
            self.say("✓ 任务 %s「%s」已标记完成（只改本地账本：没勾 Focusmate、"
                     "没当场写 OmniFocus；之后同步仍会按单向契约写回）%s"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                        ("；" + note_sub) if note_sub else ""),
                     "ok")
        else:
            task = self.task_store.drop(task_id, reason="cmd-mark-discard")
            self.say("✗ 任务 %s「%s」已标记放弃（只改本地账本：Focusmate / OmniFocus 都没动）"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40)),
                     "ok")
        self.selected_task_id = task_id
        return task

    def _dispatch_task(self, action, rest):
        # `task 3 duplicate -a 1`：动作写在序号后面（需求里的语法就是这个顺序）。
        # `id:15` 也走这条：它是「明说按内部 id」的写法（面板上没有的任务）。
        numeric = action.isdigit() or action.startswith("id:")
        if numeric and rest:
            verb = str(rest[0]).lower()
            if verb in ("duplicate", "dup", "copy", "复制"):
                return self.cmd_task_duplicate([action] + rest[1:])
            if verb in ("drop", "放弃"):
                return self.cmd_task_drop([action] + rest[1:])
            if verb in ("done", "complete", "完成"):
                return self.cmd_task_done([action] + rest[1:])
            if verb in ("link", "关联"):
                return self.cmd_task_link([action] + rest[1:])
            if verb in ("swap", "调换", "换位"):
                return self.cmd_task_swap([action] + rest[1:])
            if verb in ("assign-count", "assign_count", "act", "本场count"):
                return self.cmd_task_assign_count([action] + rest[1:])
        if action == "duplicate" or action in ("dup", "copy", "复制"):
            return self.cmd_task_duplicate(rest)
        if action == "import" or action in ("粘贴", "导入"):
            # 后缀（-a / -n / -m / -t / -f）由 cmd_task 在这之后统一套，而且会
            # **逐条**套到整批上；这里再套一遍就是双份（`-a` 会重复挂一场）。
            return self.cmd_task_import(rest, apply_suffix=False)
        if action == "create":
            return self.cmd_task_create(rest)
        if action in ("drop", "放弃"):
            return self.cmd_task_drop(rest)
        if action in ("done", "complete", "完成"):
            return self.cmd_task_done(rest)
        if action in ("link", "关联"):
            return self.cmd_task_link(rest)
        if action in ("swap", "调换", "换位"):
            return self.cmd_task_swap(rest)
        if action == "delete":
            return self.cmd_task_delete(rest)
        if action == "modify":
            return self.cmd_task_modify(rest)
        if action in ("note", "notes", "备注"):
            return self.cmd_task_note(rest)
        if action == "assign":
            return self.cmd_task_assign(rest)
        if action in ("assign-count", "assign_count", "act", "本场count"):
            return self.cmd_task_assign_count(rest)
        if action in ("archive", "history", "归档"):
            return self.cmd_task_archive(rest)
        if action in ("repeat", "recurring"):
            return self.cmd_task_repeat(rest)
        if action == "select":
            return self.cmd_task_select(rest)
        if action in ("sync", "refresh", "reconcile"):
            return self.cmd_task_sync(quiet=False)
        if action in ("list", "ls", "show"):
            self.say("任务 %d 条；看本视图表格即可。" % len(self.task_store.list()), "info")
            return False
        if action in ("sort", "排序"):
            return self.cmd_task_sort(rest)
        if action in ("group", "grouping", "分组"):
            return self.cmd_task_group(rest)
        if action in ("search", "find", "filter", "搜索"):
            return self.cmd_task_search(rest)
        if action in ("page", "翻页"):
            return self.cmd_task_page(rest)
        # 裸序号就是 select：`task 3` ≡ `task select 3`，
        # 于是 `task 3 -a 1` = 「选中第 3 条再挂到表格 #1」。
        if numeric:
            if rest:
                hint = ("`task <项目序号> <count>` 只在 `plan` 的分配问答里才有意义 —— "
                        "先敲 `plan`。" if not self.planning else
                        "allocate 的写法是 `task <项目序号> <count>`：序号 = 项目表上的 #。")
                raise ValueError("`task %s` 就等于 `task select %s`，后面不用再跟东西；"
                                 "要挂到 session 用 -a/--assign（例如 task %s -a %s）。%s"
                                 % (action, action, action, rest[0], hint))
            return self.cmd_task_select([action])
        raise ValueError("未知 task 动作 %r（create/delete/modify/assign/assign-count/"
                         "select/repeat/duplicate/drop/done/link/import/sync/archive；"
                         "也可以直接写任务 ID，如 task 8 ≡ task select 8）" % action)

    def cmd_task_archive(self, args, limit=5):
        """task archive [N] —— 看完成归档（data/task-archive.json，只增不改）。"""
        records = [r for r in (self.task_store.load_archive().get("records") or [])
                   if isinstance(r, dict)]
        try:
            limit = int(args[0]) if args else limit
        except (TypeError, ValueError):
            raise ValueError("task archive [N]：N 是看最近几条")
        self.say("归档 %s：%d 条（只增不改；同一任务+task+完成时间只记一次）"
                 % (self.task_store.archive, len(records)), "info")
        if not records:
            self.say("   还没有归档：任务在 Focusmate 上有 completedAt 之后，task sync 会补进来。",
                     "info")
            return records
        for rec in records[-limit:]:
            fm = rec.get("focusmate") or {}
            sess = rec.get("session") or {}
            fields = rec.get("fields") or {}
            title = (fields.get("title") or fields.get("name")
                     or self.task_store._one_line(fields)[:60] or "（空任务）")
            self.say("   #%s %s ← session %s %s · 完成 %s"
                     % (rec.get("task_id") or "?", title,
                        sess.get("date") or "?", sess.get("start") or "?",
                        fm.get("completed_at") or "?"), "ok")
            note = rec.get("notes") or self.task_store.notes({"fields": fields})
            if note:
                self.say("      备注：%s" % note, "info")
        return records

    def cmd_task_create(self, args):
        """task create [id] [key=value ...] [--repeat 周几@HH:MM]

        字段不校验；给了 id 就用它。``--repeat`` 把任务标成**每周重复**：到那一周
        就自动挂到同一时刻、同一时长的那场 session 上（拆分/改时长后这一周就不挂，
        但 repeat 设置不变）。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        explicit = None
        repeat_spec = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--repeat", "-r"):
                if i + 1 >= len(toks):
                    raise ValueError("--repeat 后面要跟 周几@HH:MM（例如 mon@22:15）")
                repeat_spec = toks[i + 1]
                i += 2
                continue
            if low.startswith("--repeat="):
                repeat_spec = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        toks = kept
        if toks and re.match(r"^\d+$", toks[0]) and "=" not in toks[0]:
            explicit = int(toks[0])
            toks = toks[1:]
        fields, loose = {}, []
        for tok in toks:
            if "=" in tok:
                key, value = tok.split("=", 1)
                if key:
                    fields[key] = value
            else:
                loose.append(tok)
        if loose:
            fields.setdefault("title", " ".join(loose))
        # 直接把 OmniFocus 里的「拷贝任务」粘进 title 也认：
        #   task create [数学易错修正](omnifocus:///task/kc-4VLAE_1L)
        # 认得出 primary key 就顺手关联上（只有标题的那种粘贴就只当标题）。
        link = of.parse_link(fields.get("title") or "")
        omnifocus = None
        if link and link.get("id"):
            omnifocus = {"id": link["id"], "url": link.get("url"), "title": link.get("title")}
            if link.get("title"):
                fields["title"] = link["title"]
        task = self.task_store.create(fields, task_id=explicit, omnifocus=omnifocus)
        if omnifocus:
            self.say("🔗 顺手关联到 OmniFocus %s" % omnifocus["id"], "ok")
        if repeat_spec:
            task = self.cmd_task_repeat([str(task["id"]), repeat_spec], quiet=True)
        self.selected_task_id = task["id"]
        self.say("✅ 任务 %s 已创建（字段：%s）"
                 % (self.task_label(task), self.task_store.summarize(task) or "空"), "ok")
        return task

    def cmd_task_repeat(self, args, quiet=False):
        """task repeat <id> <周几@HH:MM|off> —— 设置 / 取消每周重复。

        不写时刻时用今天最后一场的时间当锚点；duration 取那场 session 的时长，
        所以它只在「同一时刻、同一时长」的 session 上落地。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task repeat <任务序号> <周几@HH:MM|off>")
        task = self.resolve_task(toks[0])
        if len(toks) == 1:
            rep = task.get("repeat")
            if not rep:
                self.say("任务 %s 不是重复任务。" % self.task_label(task), "info")
            else:
                self.say("任务 %s 重复：%s；落点 %d 次"
                         % (self.task_label(task), ts.repeat_summary(rep),
                            len(rep.get("occurrences") or {})), "info")
                if rep.get("rule"):
                    self.say("   RRULE：%s（锚点 %s）"
                             % (rep.get("rule"), rep.get("rule_anchor") or "?"), "info")
            return task
        spec = toks[1].lower()
        if spec in ("off", "none", "clear", "cancel"):
            task = self.task_store.clear_repeat(task["id"])
            self.say("✅ 任务 %s 的每周重复已取消。" % self.task_label(task), "ok")
            return task
        parsed = cfg.parse_repeat_spec(toks[1])
        # 锚点：同一天里同一时刻那场 session 的时长（找不到就用 50）
        day = datetime.date.today()
        day += datetime.timedelta(days=(parsed["weekday"] - day.weekday()) % 7)
        anchored = None
        for sess in self.sessions_for_day_plan(day):
            if sess.get("start") == parsed["start"]:
                anchored = sess
                break
        duration = int(anchored["duration"]) if anchored else 50
        task = self.task_store.set_repeat(task["id"], parsed["weekday_label"],
                                         parsed["start"], duration)
        if not quiet:
            self.say("✅ 任务 %s 每周 %s %s 重复（锚定 %dmin 那场）"
                     % (self.task_label(task), parsed["weekday_label"],
                        parsed["start"], duration), "ok")
        return task

    def cmd_task_note(self, args):
        """task note <任务 ID> <备注文本>；不写文本 = 清空。

        备注可以是网址（完成之后照着去对应地方把活干完），也可以是任何话。
        """
        if not args:
            raise ValueError("用法：task note <任务 ID> <备注文本>（不写文本 = 清空）")
        text = " ".join(str(x) for x in args[1:]).strip()
        task = self.task_store.set_notes(self.resolve_task(args[0])["id"], text)
        self.say("✅ 任务 %s 备注%s：%s"
                 % (self.task_label(task), "已清空" if not text else "",
                    self.task_store.notes(task) or "（空）"), "ok")
        return task

    def cmd_task_delete(self, args):
        if not args:
            raise ValueError("用法：task delete <任务序号>")
        task = self.task_store.delete(self.resolve_task(args[0])["id"])
        if self.selected_task_id == task["id"]:
            self.selected_task_id = None
        self.say("✅ 已删除任务 %s：%s"
                 % (self.task_label(task), self.task_store.summarize(task)), "ok")
        return True

    def cmd_task_modify(self, args):
        if len(args) < 3:
            raise ValueError("用法：task modify <任务序号> <字段> <新值>")
        task_id, field = self.resolve_task(args[0])["id"], args[1]
        value = " ".join(str(x) for x in args[2:])
        task = self.task_store.modify(task_id, field, value)
        self.say("✅ 任务 %s.%s = %r" % (self.task_label(task), field, value), "ok")
        return task

    def cmd_task_select(self, args):
        if not args:
            raise ValueError("用法：task select <任务序号>")
        task = self.resolve_task(args[0])
        self.selected_task_id = task["id"]
        self.task_view_page = None
        self.say("已选中任务 %s：%s"
                 % (self.task_label(task), self.task_store.summarize(task)), "cmd")
        return task

    def _task_for_assign(self):
        if self.selected_task_id is not None:
            try:
                return self.task_store.get(self.selected_task_id)
            except ValueError:
                self.selected_task_id = None
        tasks = self.task_store.list()
        if not tasks:
            raise ValueError("还没有任务：先 task create ...")
        # 没选中时默认最近创建的那条。想精确指定就 task select / assign <任务ID> <sessionID>。
        task = tasks[-1]
        self.selected_task_id = task["id"]
        return task

    def cmd_task_duplicate(self, args):
        """``task <id> duplicate [-a N]`` —— 复制一条本地任务，指向同一条 OmniFocus 任务。

        需求（第 4 条）的原话就是 ``task {index} duplicate``（可以接 ``-a`` 之类）。
        复制品是一条**新的本地记录**：字段、本地标签、OmniFocus primary key 全一样，
        但 ID 是新的、session 是空的（除非 ``-a``）。于是「一个 OmniFocus 任务 ↔
        多条本地记录」成为可能 —— 这正是「单向覆盖」的前提：OmniFocus 只管有活没活，
        哪一场在干、干没干由本地账本决定。

        不带 ``-a`` 时复制品不挂场次（先放着，之后 ``task N -a M``）。
        原来的那条**不动**：要让它「变成放弃 + 未完成转移」是收尾里 A 那一步的事。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <任务序号> duplicate [-a <session ID>]")
        task = self.resolve_task(toks[0])
        copy = self.task_store.duplicate(task["id"])
        self.selected_task_id = copy["id"]
        self.say("✅ 任务 %s「%s」→ 复制成 %s（同一条 OmniFocus：%s）"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    self.task_label(copy),
                    (task.get("omnifocus") or {}).get("id") or "未关联"), "ok")
        return copy

    def cmd_task_drop(self, args):
        """``task <id> drop [--local]`` —— 放弃：本地放弃，默认**OmniFocus 也放弃**。

        ``--local`` 只在本地放弃（「延期 / 转移动了账本」那种语义），OmniFocus 不动。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        local_only = "--local" in toks
        toks = [t for t in toks if t != "--local"]
        if not toks:
            raise ValueError("用法：task <任务序号> drop [--local]")
        task = self.resolve_task(toks[0])
        template = ts.TaskStore.is_repeat_template(task)
        if not local_only and ts.TaskStore.omnifocus_id(task):
            self.jobs.begin(1, title="放弃任务 %s（task %s drop）"
                                   % (self.task_label(task), self.task_label(task)))
        note = "" if local_only else self.omni_drop(task, all_occurrences=template)
        task = self.task_store.set_status(task["id"], ts.STATUS_DROPPED, reason="cmd-drop")
        self.say("✗ 任务 %s「%s」已放弃%s%s"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    ("；" + note) if note else "（只改了本地）",
                    "；这是重复任务本体，之后不再生成子任务" if template else ""), "ok")
        return task

    def cmd_task_done(self, args):
        """``task <id> done [分钟]`` —— 完成：本地完成 + Focusmate 勾上 + OmniFocus 写回。

        不给分钟数也行；给了就记成实际耗时（和收尾里「缺漏完成补充」同一套）。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <任务序号> done [实际分钟数]")
        task = self.resolve_task(toks[0])
        if ts.TaskStore.is_repeat_template(task):
            raise ValueError("任务 %s 是重复任务**本体**，不能当成一次 session 的活来完成；"
                             "它能生成的子任务才是能完成的。要结束这套重复："
                             "`task repeat %s off`（保留任务）或 `task %s drop`（手动放弃）。"
                             % (self.task_label(task), self.task_label(task),
                                self.task_label(task)))
        minutes = None
        if len(toks) > 1:
            try:
                minutes = int(toks[1])
            except ValueError:
                raise ValueError("分钟数得是整数；你给的是 %r" % toks[1]) from None
        return self._complete_task(task, minutes=minutes, why="cmd-done")

    def _project_child_completed(self, task, writeback=True):
        """本地子任务完成后，更新项目库；全部子任务完成 → 父任务自动完成。

        ``writeback=True`` 时把父任务立刻写回 OmniFocus（普通 ``task done`` /
        收尾完成走这条）；``-m complete`` 这种「只动本地账本」的路径传 False。
        """
        fields = (task or {}).get("fields") or {}
        source = (task or {}).get("source") or {}
        parent_pk = str(source.get("parent_id") or "").strip()
        child_pk = str(source.get("id") or fields.get(pj.PROJECT_TASK_FIELD)
                       or "").strip()
        if not parent_pk or not child_pk:
            return ""
        try:
            project_id = int(fields.get(pj.PROJECT_ID_FIELD))
        except (TypeError, ValueError):
            return ""
        try:
            project, auto_done, newly = self.projects.complete_subtasks(
                project_id, parent_pk, [child_pk])
        except (ValueError, TypeError) as e:
            self.say("⚠ 更新项目子任务完成状态失败：%s" % e, "warn")
            return ""
        parent = pj.task_by_pk(project, parent_pk) or {}
        parent_title = parent.get("title") or parent_pk
        notes = []
        if newly:
            notes.append("项目「%s」子任务「%s」已记完成"
                         % (project.get("title") or project_id,
                            fields.get("title") or child_pk))
        if auto_done:
            notes.append("父任务「%s」的子任务全部完成 → 父任务自动完成"
                         % parent_title)
            if writeback:
                if self.omnifocus_enabled():
                    action = self.net_action(
                        "omnifocus.complete",
                        "OmniFocus 完成父任务 %s" % parent_pk[:8],
                        {"pk": parent_pk, "at": None, "due": None},
                        source=SRC_OMNIFOCUS)
                    job = self.jobs.submit(
                        action,
                        on_fail=lambda error: self.say(
                            "父任务「%s」自动完成了本地的，但 OmniFocus 写回失败：%s"
                            % (parent_title, error), "warn"))
                    if self.jobs.sync:
                        outcome = job.outcome
                        if outcome is None or not outcome.ok:
                            notes.append("OmniFocus 父任务写回失败：%s"
                                         % (outcome and outcome.error or "没跑"))
                        else:
                            notes.append("OmniFocus 父任务已完成")
                    else:
                        notes.append("OmniFocus 父任务完成写回丢后台了")
                else:
                    notes.append("OmniFocus 关着 → 父任务只完成了本地")
        if notes:
            self.rebuild_rows()
            self.invalidate()
        return "；".join(notes)

    def _complete_task(self, task, minutes=None, why="cmd-done"):
        """完成任务的那一段（``task done`` / ``-c`` 够数 / 收尾 F 共用）。

        一次操作、最多两步：先勾 Focusmate（那才是「这一场干完了」的事实），
        再写回 OmniFocus。面板标题是这次操作本身。
        """
        if ts.TaskStore.is_repeat_template(task):
            raise ValueError("任务 %s 是重复任务**本体**，不能当成一次 session 的活来完成。"
                             % self.task_label(task))
        has_fm = bool((task.get("focusmate") or {}).get("task_id"))
        has_omni = bool(ts.TaskStore.omnifocus_id(task))
        self.jobs.begin(2 if (has_fm and has_omni) else 1,
                        title="完成任务 %s（task %s done）"
                              % (self.task_label(task), self.task_label(task)))
        task = self.task_store.complete(task["id"], minutes=minutes, reason=why)
        self.task_store.archive_local(self.task_store.get(task["id"]))
        note_fm = self.fm_complete(task)
        note_of = self.omni_complete(task)
        note_sub = self._project_child_completed(task, writeback=True)
        self.say("✓ 任务 %s「%s」完成%s%s%s%s"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    ("（%d 分钟）" % minutes) if minutes else "",
                    ("；" + note_fm) if note_fm else "",
                    ("；" + note_of) if note_of else "",
                    ("；" + note_sub) if note_sub else ""), "ok")
        return task

    def cmd_task_link(self, args):
        """``task <id> link [链接|PK]`` —— 把本地任务关联到 OmniFocus。

        **不给链接就是读剪贴板**（和 `task import` 同一条规矩：动作归子命令）。
        OmniFocus 里 ⌘C 那条任务，回来敲 `task 5 link` 就完事。

            task 5 link                 ← 读剪贴板（只认里面唯一的那条链接）
            task 5 link omnifocus:///task/kc-4VLAE_1L
            task 5 link "[数学易错修正](omnifocus:///task/kc-4VLAE_1L)"
            task 5 link kc-4VLAE_1L

        只有标题（``title: "数学易错修正"``）那种粘贴**认不出 primary key**，
        会明确拒绝 —— 拿标题去猜一条任务，猜错比没有更糟。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <任务 ID> link [omnifocus:///task/PK | PK]")
        task_id = self.resolve_task(toks[0])["id"]
        rest = toks[1:]
        auto = not rest
        if auto:
            # 读剪贴板走 worker（osascript / pbpaste 也是外部操作）。
            # 这里不能先读：用户敲完 link 应该立刻回到提示符。
            self.jobs.begin(1, title="关联 OmniFocus（task %s link）"
                                   % self.task_label(self.task_store.get(task_id)))
            holder = {}

            def apply_links(payload):
                items = [item for item in ((payload or {}).get("items") or [])
                         if item.get("id")]
                if not items:
                    self.had_error = True
                    self.say("剪贴板里没有 OmniFocus 链接（URL / HTML / RTF / 纯文本 "
                             "都读了）。在 OmniFocus 里 ⌘C 那条任务再来。", "error")
                    self.invalidate()
                    return
                if len(items) > 1:
                    self.had_error = True
                    self.say("剪贴板里有 %d 条 OmniFocus 链接：%s —— 只拷一条再来，"
                             "或者把要的那条的链接直接写出来。"
                             % (len(items), "、".join(i["id"] for i in items[:5])),
                             "error")
                    self.invalidate()
                    return
                info = items[0]
                task = self.task_store.link_omnifocus(
                    task_id, info["id"], title=info.get("title"))
                holder["task"] = task
                self.say("🔗 任务 %s 已关联 OmniFocus %s%s（链接是从剪贴板读的）"
                         % (self.task_label(task), info["id"],
                            ("「%s」" % info["title"]) if info.get("title") else ""), "ok")

            action = self.net_action(
                "omnifocus.clipboard", "读剪贴板里的 OmniFocus 链接", {},
                source=SRC_OMNIFOCUS)
            self.jobs.submit(
                action, persist=False, on_ok=apply_links,
                on_fail=lambda error: (setattr(self, "had_error", True),
                                       self.say("读剪贴板失败：%s" % error, "error")))
            if self.jobs.sync:
                return holder.get("task")
            self.say("正在后台读剪贴板里的 OmniFocus 链接…", "cmd")
            return NET_PENDING
        info = of.parse_link(" ".join(rest))
        if not info or not info.get("id"):
            raise ValueError("认不出 OmniFocus 任务 primary key：%r（要 omnifocus:///task/… "
                             "或裸 PK；只有标题的那种粘贴认不出来）" % (" ".join(rest),))
        task = self.task_store.link_omnifocus(task_id, info["id"], title=info.get("title"))
        self.say("🔗 任务 %s 已关联 OmniFocus %s%s"
                 % (self.task_label(task), info["id"],
                    ("「%s」" % info["title"]) if info.get("title") else ""), "ok")
        return task

    def _answer_pending_import(self, line):
        """用户在「复制重复设置吗？」后敲的 y / n / q。"""
        text = " ".join(str(line or "").strip().lower().split())
        yes = {"y", "yes", "是", "要", "复制", "copy", "ok"}
        no = {"n", "no", "不", "否", "不要", "只导入", "skip"}
        cancel = {"q", "quit", "cancel", "取消", "abort", "esc"}
        if text in cancel:
            self.pending_import = None
            self.say("已取消这次导入；没有建本地任务。", "warn")
            self.invalidate()
            return True
        if text not in yes and text not in no:
            self.say("导入还在等回答：[y] 复制重复设置并导入 / [n] 只导入这一次 / [q] 取消",
                     "warn")
            return True

        pending, self.pending_import = self.pending_import, None
        try:
            task = self._finish_task_import(
                pending.get("items") or [],
                copy_repeat=(text in yes),
                repeats=pending.get("repeats") or {},
                warnings=pending.get("warnings") or [],
            )
            self._apply_import_suffix(task, pending.get("suffix"))
        except ValueError as e:
            self.had_error = True
            self.say(str(e), "warn")
        except Exception as e:                          # noqa: BLE001 - 别让问句把 TUI 掀了
            self.had_error = True
            self.say("导入失败：%s: %s" % (type(e).__name__, e), "error")
        try:
            self.refresh_warnings(force=True)
        except Exception:                               # noqa: BLE001 - 警告板坏了也不能丢导入结果
            pass
        self.invalidate()
        return True

    def _apply_suffix_chain(self, task, action, note_text=None, assign_to=None,
                            exclusive=None, mark_status=None, count_spec=None,
                            assign_count_spec=None, trigger_spec=None,
                            formatter_spec=None):
        """按固定顺序把一条任务的后缀跑完：
        note → count → exclusive → assign → mark → trigger → formatter → assign-count。

        count 排在 formatter 前面是有意的：``task create ... -c 3 -f end`` 里
        ``-f end`` 要按「刚刚设好的时长」去切 session（count 3 = 75 分钟）。
        assign-count 排在最后，是为了让 ``-ac -act 2``（以及 formatter 重新
        落场）改到的都是**最终**那一场。

        这是 `cmd_task` 和 `task import` 共用的那一段；一次导入多条时由
        :meth:`_apply_import_suffix` 对**每一条**各跑一遍。
        """
        if note_text is not None:
            self._apply_note(task, note_text, action)
        if count_spec is not None:
            task = self._apply_count(task, count_spec, action) or task
        if exclusive is not None:
            task = self._apply_exclusive(task, exclusive, action) or task
        if assign_to is not None:
            task = self._apply_assign(task, assign_to, action) or task
        if mark_status is not None:
            task = self._apply_mark(task, mark_status, action) or task
        if trigger_spec is not None:
            task = self._apply_trigger(task, trigger_spec, action) or task
        if formatter_spec is not None:
            task = self._apply_formatter(task, formatter_spec, action) or task
        if assign_count_spec is not None:
            task = self._apply_assign_count(task, assign_count_spec, action) or task
        return task

    def _apply_import_suffix(self, task, suffix):
        """把 `task import` 后面挂的 -n / -a / -m / -act / -t / -f 落到导入任务上。

        ``task`` 可以是单条任务，也可以是 :class:`ImportBatch`（一次导入多条）——
        后一种情况后缀逐条套，不是只套最后一条。重复任务本体会在这些动作里被导向
        当天的 occurrence 子任务（见 :meth:`_assign_task` /
        :meth:`_apply_formatter`），所以 ``task import -ac`` 不会把模板本身挂到
        session 上。
        """
        suffix = suffix if isinstance(suffix, dict) else {}
        if isinstance(task, ImportBatch):
            targets = [one for one in task
                       if isinstance(one, dict) and one.get("id") is not None]
            last = None
            for one in targets:
                last = self._apply_suffix_chain(
                    one, "import", note_text=suffix.get("note_text"),
                    assign_to=suffix.get("assign_to"),
                    exclusive=suffix.get("exclusive"),
                    mark_status=suffix.get("mark_status"),
                    count_spec=suffix.get("count_spec"),
                    assign_count_spec=suffix.get("assign_count_spec"),
                    trigger_spec=suffix.get("trigger_spec"),
                    formatter_spec=suffix.get("formatter_spec"))
            return last if last is not None else task.last
        if not isinstance(task, dict) or task.get("id") is None:
            return task
        return self._apply_suffix_chain(
            task, "import", note_text=suffix.get("note_text"),
            assign_to=suffix.get("assign_to"),
            exclusive=suffix.get("exclusive"),
            mark_status=suffix.get("mark_status"),
            count_spec=suffix.get("count_spec"),
            assign_count_spec=suffix.get("assign_count_spec"),
            trigger_spec=suffix.get("trigger_spec"),
            formatter_spec=suffix.get("formatter_spec"))

    def _finish_task_import(self, items, copy_repeat=False, repeats=None,
                            warnings=None):
        """把（已经问过重复设置的）候选真正写进本地任务库。"""
        repeats = repeats or {}
        for warning in warnings or []:
            self.say(str(warning), "warn")
        made = []
        for index, item in enumerate(items or []):
            if not item:
                continue
            title = item.get("title") or (item.get("id") or "（无标题）")
            rec = repeats.get(index) or {}
            plan = rec.get("plan") or {}
            fields = {"title": title}
            # OmniFocus 的 estimatedMinutes 就是「这条活要多久」；导入时落到
            # fields.duration，formatter end 才有东西可排（不像 repeat.duration
            # 只是找 session 的锚点）。用户已经手写过就不覆盖。
            # 来源顺序：重复任务 plan 里的时长 → OmniFocus 查回来的 estimated_minutes。
            minutes = None
            for raw_minutes in (plan.get("duration"), item.get("estimated_minutes")):
                try:
                    minutes = int(raw_minutes)
                except (TypeError, ValueError):
                    minutes = None
                if minutes and minutes > 0:
                    break
            if minutes and minutes > 0:
                fields["duration"] = str(minutes)
            if item.get("project") and not fields.get(pj.PROJECT_FIELD):
                fields[pj.PROJECT_FIELD] = str(item["project"]).strip()
            omnifocus = None
            if item.get("id"):
                omnifocus = {"id": item["id"], "url": item.get("url"),
                             "title": item.get("title")}
            source = ({"kind": "omnifocus", "id": item["id"],
                       "url": item.get("url") or "omnifocus:///task/%s" % item["id"],
                       "title": item.get("title")}
                      if item.get("id") else None)
            task = self.task_store.create(fields, omnifocus=omnifocus,
                                          source=source)
            made.append(task)
            if item.get("id"):
                notes = []
                if item.get("title_from_omnifocus"):
                    notes.append("标题是去 OmniFocus 查的")
                if item.get("from_clipboard"):
                    notes.append("链接是从剪贴板读的")
                self.say("✅ 导入任务 %s「%s」→ OmniFocus %s%s"
                         % (self.task_label(task), title, item["id"],
                            "（%s）" % "；".join(notes) if notes else ""), "ok")
            else:
                self.say("✅ 导入任务 %s「%s」（只有标题：认不出 OmniFocus primary key，"
                         "所以**没有关联**）" % (self.task_label(task), title), "warn")
                self.say("   要连上那条 OmniFocus 任务：回 OmniFocus ⌘C 它，"
                         "再敲 `task %s link`（不带参数就是读剪贴板）。"
                         % self.task_no(task), "info")

            if not copy_repeat or not rec:
                continue
            if not plan.get("supported"):
                self.say("⚠ 任务 %s 是重复任务，但这条规则复制不了：%s"
                         % (self.task_label(task), plan.get("reason") or "规则无法映射"),
                         "warn")
                continue
            try:
                self.task_store.set_repeat(
                    task["id"], plan["weekday_label"], plan["start"],
                    plan.get("duration"),
                    rule=plan.get("rule"),
                    rule_anchor=plan.get("rule_anchor"),
                    rule_offset_days=plan.get("rule_offset_days") or 0,
                    source="omnifocus" if plan.get("rule") else None,
                    summary=plan.get("summary"),
                    meta={k: (rec.get("info") or {}).get(k)
                          for k in ("method", "schedule_type", "anchor_date_key",
                                    "catch_up_automatically", "next")},
                )
            except ValueError as e:
                self.say("⚠ 复制重复设置失败：%s（任务已按普通任务导入）" % e, "warn")
                continue
            task = self.task_store.get(task["id"])
            self.say("🔁 已复制重复设置：任务 %s %s"
                     % (self.task_label(task), plan.get("summary") or ""), "ok")
            if plan.get("note"):
                self.say("   %s" % plan["note"], "info")
        if made:
            self.selected_task_id = made[-1]["id"]
        if len(made) > 1:
            linked = [t for t in made if ts.TaskStore.omnifocus_id(t)]
            copied = [t for t in made if t.get("repeat")]
            self.say("📥 这一批导入 %d 条：关联 OmniFocus %d 条、无关联 %d 条%s"
                     % (len(made), len(linked), len(made) - len(linked),
                        "；复制了重复设置 %d 条" % len(copied) if copied else ""), "ok")
            rl.log_event("omnifocus", "import_batch", count=len(made),
                         linked=len(linked), repeats=len(copied),
                         task_ids=[t.get("id") for t in made],
                         omnifocus_ids=[ts.TaskStore.omnifocus_id(t) for t in made])
            return ImportBatch(made)
        return made[-1] if made else None

    def cmd_task_import(self, args, apply_suffix=True):
        """``task import [--assign N] [粘贴文本|链接]`` —— 从**剪贴板**导入任务。

        需求（第 4 条）：在 OmniFocus 里复制一条任务（剪贴板里自带 primary key 的
        链接），然后 import —— 本地任务**自动关联**到那条 OmniFocus 任务。

        **动作归子命令，后缀归分流**：

            task import                 ← 读剪贴板（默认动作，不需要 flag）
            task import -a 3            ← 同上，顺手分流/挂到表格 #3
            task import -st             ← 只导入这一条，不问重复设置、不建 repeat
            task import [标题](omnifocus:///task/PK)   ← 手动给链接

        为什么不靠粘贴：从 OmniFocus ⌘C 再往终端 ⌘V，粘出来的**只有标题** ——
        终端只接受纯文本那个 flavor，链接在粘贴那一刻就没了。所以 import 自己按
        flavor 去读剪贴板：`the clipboard as URL` / `«class HTML»` / `«class RTF »` /
        纯文本，四个都读、按 id 合并（见 ``omnifocus.clipboard_links``）。

        不带参数就是读剪贴板；粘进来一段**只有标题**的文字时，会去剪贴板里对一下
        标题完全一致的那条链接（那是同一次 ⌘C 的两半，不是拿标题猜身份），对不上
        就建任务但**明确说一句**没关联 —— 宁可少关联，也不拿标题猜身份。

        如果剪贴板那条在 OmniFocus 里带 repetitionRule（每周三之类），**先不建**：
        把候选挂到 ``pending_import``，由打断栏位问「复制重复设置吗？」；用户答
        ``y`` 才把可表达的规则写成本地 ``repeat``，答 ``n`` 只导入这一次。

        **一次导入多条**：在 OmniFocus 里多选几条 ⌘C，剪贴板里就是多条链接。
        这里对每一条都做「关联 + 标题 + 时长」，返回 :class:`ImportBatch`
        （单条时还是那一条任务），``cmd_task`` 的 ``-a`` / ``-f`` 等后缀因此会落到
        整批上。剪贴板给的 flavor 可能只给链接不给标题（多选时 RTF 与纯文本是两份
        互不相干的东西），所以有 pk 没标题的条目会去 OmniFocus 按 pk 查真名
        （顺便带上 ``estimated_minutes`` → ``fields.duration``），补完再按标题去重。
        """
        toks = []
        assign_to = None
        single_task = False
        op_type = None
        advance_pro_id = None
        if self.pending_advance is not None:
            advance_pro_id = self.pending_advance.get("pro_id")
            op_type = self.pending_advance.get("required_op_type")
        raw = [str(a).strip() for a in args]
        i = 0
        while i < len(raw):
            tok, low = raw[i], str(raw[i]).lower()
            if low in ("--single-task", "-st"):
                single_task = True
            elif low in ("--assign", "-a") and i + 1 < len(raw):
                assign_to = raw[i + 1]
                i += 2
                continue
            elif low.startswith("--assign="):
                assign_to = tok.split("=", 1)[1]
            elif low in ("--type", "--kind") and i + 1 < len(raw):
                parsed_t = pj.normalize_op_type(raw[i + 1])
                if parsed_t is None:
                    raise ValueError("OmniFocus 项目（op）类型只能是 SOP 或 incubator；你给的是 %r" % raw[i + 1])
                op_type = parsed_t
                i += 2
                continue
            elif low.startswith("--type=") or low.startswith("--kind="):
                val = tok.split("=", 1)[1]
                parsed_t = pj.normalize_op_type(val)
                if parsed_t is None:
                    raise ValueError("OmniFocus 项目（op）类型只能是 SOP 或 incubator；你给的是 %r" % val)
                op_type = parsed_t
            elif low in ("--sop", "sop"):
                op_type = pj.OP_TYPE_SOP
            elif low in ("--incubator", "incubator", "inc"):
                op_type = pj.OP_TYPE_INCUBATOR
            elif tok:
                toks.append(tok)
            i += 1

        pasted = " ".join(toks).strip()
        read_clipboard = False
        match_clipboard = False
        if not pasted:
            # 空的 items + clipboard 标记：真正读剪贴板放到 worker 里做。
            items = []
            read_clipboard = True
        else:
            items = of.parse_links(pasted) or [of.parse_link(pasted)]
            if not any(item and item.get("id") for item in items):
                # 粘进来的只有标题 → 到 worker 里对剪贴板同名的那条
                # （同一次 ⌘C 的两半）。
                match_clipboard = True

        if single_task:
            self.say("📥 按 -st/--single-task 只导入这一条；不去核对重复设置，"
                     "也不会建本地 repeat。", "cmd")

        # ── 一次操作，两步 ────────────────────────────────────────────
        #   1) [omnifocus] 获取任务   ← 补标题/时长 + 核对重复设置（读）
        #   2) [focusmate] 挂任务     ← 只有 -a 才有（写，乐观）
        # 面板标题是**这个操作**（import 任务（task import -ac）），不是某一步。
        suffix = dict(getattr(self, "_import_suffix_doc", None) or {})
        if assign_to is not None and not suffix:
            suffix = {"assign_to": assign_to}
        title = "import 任务（task import%s）" % self._suffix_summary(suffix)
        self.jobs.begin(2 if suffix.get("assign_to") is not None else 1, title=title)
        action = self.net_action("omnifocus.import", "获取任务",
                                 {"items": items, "single_task": single_task,
                                  "suffix": suffix,
                                  "op_type": op_type,
                                  "advance_pro_id": advance_pro_id,
                                  "clipboard": read_clipboard,
                                  "match_clipboard": match_clipboard,
                                  # 同步模式由 cmd_task 统一套后缀（这条路径原来就这样）
                                  "apply_suffix": False}, source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action)
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return None
            return getattr(self, "_last_import_result", None)
        return NET_PENDING

    @staticmethod
    def _suffix_summary(suffix):
        """后缀 → 面板标题里那一小段（``-a 3 -f end``），只为好看。"""
        suffix = suffix or {}
        bits = []
        if suffix.get("assign_to") is not None:
            bits.append("-a %s" % suffix["assign_to"])
        if suffix.get("applied") is True:          # 内部占位，永不显示
            pass
        if suffix.get("exclusive"):
            bits.append("-e true")
        if suffix.get("note_text"):
            bits.append("-n …")
        formatter = suffix.get("formatter_spec") or {}
        if formatter.get("name"):
            bits.append("-f %s" % formatter["name"])
        mark = suffix.get("mark_status")
        if mark:
            bits.append("-m %s" % mark)
        trigger = suffix.get("trigger_spec") or {}
        if trigger.get("name"):
            bits.append("-t %s" % trigger["name"])
        return (" " + " ".join(bits)) if bits else ""

    def _task_import_omnifocus_rows(self, items, warn=None):
        """这一批 pk → OmniFocus 的行（一次 list，client 里 20 秒缓存）；读不到 None。

        只在 import 里用。读不到不是错误：标题退回剪贴板给的（可能为空），时长就
        不补 —— 导入本身照常进行。
        """
        say = warn or (lambda text, kind="warn": self.say(text, kind))
        pks = {str(item.get("id")) for item in items or () if item.get("id")}
        if not pks:
            return {}
        try:
            enabled = self.omnifocus_enabled()
        except Exception:                          # noqa: BLE001 - 替身/坏设置都不该挡住导入
            enabled = False
        if not enabled:
            if any(item.get("id") and not str(item.get("title") or "").strip()
                   for item in items or ()):
                say("没在读 OmniFocus：剪贴板没给标题的那几条先拿 primary key "
                    "当标题，时长也补不上。", "info")
            return None
        try:
            rows = self.omnifocus.list_tasks()
        except Exception as e:                      # noqa: BLE001 - 读失败不该挡住导入
            say("读 OmniFocus 失败（%s）：标题按剪贴板给的用，时长不补。" % e, "warn")
            return None
        out = {}
        for row in rows or ():
            pk = str(row.get("id") or "")
            if pk and pk in pks:
                out[pk] = row
        return out

    def _task_import_resolve(self, items, warn=None):
        """补齐标题/时长，并把「另一份 flavor 抄了一遍」的裸标题去掉。

        多选 ⌘C 时剪贴板里其实是两份东西：RTF/HTML 给「链接 + 标题」，纯文本把
        那一串标题**再抄一遍**。所以：

        1. 有 pk 但没标题（RTF 只给链接、或 HTML flavor 缺）→ 去 OmniFocus 按 pk
           查真名（``name``），顺手把 ``estimated_minutes`` 带上（导入时落成
           ``fields.duration``，``-f end`` 才有东西可排）；
        2. 标题比 OmniFocus 里的短（flavor 截断，真实反馈过「语法高频 30 个」只给
           「30」）→ 用长的那个；一样长就保留剪贴板的（flavor 优先）；
        3. 补完标题再按「完全相同」去重：纯文本那份裸标题会被清掉，不会 3 条变 6 条。
        """
        items = [item for item in (items or []) if item]
        if not items:
            return []
        rows = self._task_import_omnifocus_rows(items, warn=warn)
        if rows:
            for item in items:
                pk = item.get("id")
                if not pk:
                    continue
                row = rows.get(str(pk))
                if not row:
                    continue
                name = str(row.get("name") or "").strip()
                old = str(item.get("title") or "").strip()
                if name and len(name) > len(old):
                    item["title"] = name
                    item["title_from_omnifocus"] = True
                minutes = row.get("estimated_minutes")
                if minutes not in (None, "", 0, "0"):
                    item["estimated_minutes"] = minutes
                proj_name = str(row.get("project") or "").strip()
                if proj_name and not item.get("project"):
                    item["project"] = proj_name
        return of.drop_duplicate_titles(items)

    def _task_import_skip_known(self, items):
        """去掉「这个 pk 已经和本地某条任务关联过」的条目。

        多选很容易把上个月导入过的那几条再选一遍；重复建任务（同一个 pk 两条本地
        记录）会让两边的写回互相打脸。所以跳过并明说，不静默。返回
        ``(要导入的, [(pk, 已有任务 id), ...])``。

        **只在批量导入里用**（``len(items) > 1``）：单条导入是用户明确要的东西。
        """
        known = {}
        for task in self.task_store.list():
            pk = ts.TaskStore.omnifocus_id(task)
            if pk:
                known.setdefault(str(pk), task)
        kept, skipped = [], []
        for item in items or ():
            pk = str(item.get("id") or "")
            hit = known.get(pk) if pk else None
            if hit is not None:
                skipped.append((pk, hit.get("id")))
                continue
            kept.append(item)
        if skipped:
            self.say("⏭ 跳过 %d 条已经导入过的：%s（想再来一条就 `task <序号> duplicate`）"
                     % (len(skipped),
                        "、".join("OmniFocus %s → 任务 %s" % pair for pair in skipped)),
                     "warn")
        return kept, skipped

    @staticmethod
    def _task_import_match_clipboard(items, clip_items):
        """粘贴的「只有标题」→ 去剪贴板里找同名的那条，认下它的 id。

        只在**标题完全一致**（去掉首尾空白/合并空格后）时才认：这是同一次 ⌘C
        的两半，不是拿标题猜身份。认不出来就原样返回，由调用方明说「没关联」。
        """
        def norm(text):
            return " ".join(str(text or "").split()).strip().lower()

        wanted = [norm((it or {}).get("title")) for it in items if it]
        by_title = {}
        for cand in clip_items or []:
            key = norm(cand.get("title"))
            if key and cand.get("id"):
                by_title.setdefault(key, cand)
        out = []
        for item, key in zip([it for it in items if it], wanted):
            hit = by_title.get(key)
            if hit and not item.get("id"):
                merged = dict(item)
                merged["id"] = hit["id"]
                merged["url"] = hit.get("url")
                merged["title"] = item.get("title") or hit.get("title")
                merged["from_clipboard"] = True
                out.append(merged)
            else:
                out.append(item)
        return out

    # -- 导入：剪贴板里可能是一个 **项目** ---------------------------------

    def _project_repeat_infos(self, payload, warn=None):
        """查项目清单里每条任务的 OmniFocus 重复规则 → ``{pk: info}``。

        重复任务的未来 occurrence 现在还不存在，``list_tasks`` 看不到；必须逐条
        问 ``repetitionInfo``，才能在导入时把「本周会产生几条」算进去。查询失败
        只警告、按普通任务算（和单条导入的既有约定一致），不拿网络故障冒充重复。
        """
        checker = getattr(self.omnifocus, "repetition_info", None)
        if checker is None:
            return {}
        say = warn or (lambda text, kind="warn": self.say(text, kind))
        infos = {}
        for item in (payload or {}).get("tasks") or ():
            pk = str(item.get("pk") or "")
            if not pk or item.get("completed") or item.get("dropped"):
                continue
            try:
                info = checker(pk)
            except Exception as e:              # noqa: BLE001 - 核对不了就按普通任务算
                say("没法核对「%s」是不是重复任务：%s（先按普通任务算）"
                    % (item.get("title") or pk, e), "warn")
                continue
            if isinstance(info, dict) and info.get("repeated"):
                infos[pk] = info
        return infos

    def _task_import_projects(self, items, warn=None):
        """把「剪贴板里其实是 OmniFocus 项目」的条目认出来。

        返回 ``(剩下的普通条目, 项目 payload 列表, 被拒的项目 payload 列表)``。

        判定只靠一件事：这个 primary key 在 OmniFocus 里是一条
        ``kind == "project_root"`` 的行（项目自身的根任务）。**不拿标题猜**——
        猜错了就不是「导入项目」而是「导入了一个同名任务」，两种东西的行为差得远。

        项目里**每条未完成任务都必须有「持续时间」**（OmniFocus 的 estimated
        duration）：没有时长就算不出要占几个 count，而这种项目一旦被唤起就是满地
        0 count 的任务。所以那一条项目整个**拒绝导入**（用户要求：报错，不建任何
        东西），并列出是哪几条要去补。
        """
        say = warn or (lambda text, kind="warn": self.say(text, kind))
        pks = [str(item.get("id")) for item in items or () if item.get("id")]
        if not pks:
            return list(items or ()), [], []
        try:
            enabled = self.omnifocus_enabled()
        except Exception:                          # noqa: BLE001 - 替身/坏设置都不该挡住导入
            enabled = False
        # 链接本身已经说了「这是项目」（omnifocus:///project/PK）：这时候**读不到**
        # OmniFocus 就不能装作没看见 —— 拿不到清单 = 不知道里面有哪几条任务要几个
        # count，硬导进去只会得到一条「叫项目名的任务」。
        looks_project = [item for item in items or ()
                         if str(item.get("kind") or "") == "project"]
        if not enabled:
            if looks_project:
                return list(items or ()), [], [
                    {"title": item.get("title") or item.get("id"),
                     "missing": [],
                     "reason": "「%s」是 OmniFocus **项目**链接，但 OmniFocus 集成关着"
                               "（`set omnifocus on` 打开）—— 读不到它的任务清单，"
                               "没法算 count。" % (item.get("title") or item.get("id"))}
                    for item in looks_project]
            return list(items or ()), [], []
        try:
            rows = self.omnifocus.list_tasks()
        except Exception as e:                      # noqa: BLE001 - 读不到就按普通任务导
            if looks_project:
                return list(items or ()), [], [
                    {"title": item.get("title") or item.get("id"), "missing": [],
                     "reason": "「%s」是 OmniFocus **项目**链接，但读不到 OmniFocus"
                               "（%s）—— 没法核对它的任务清单。" %
                               (item.get("title") or item.get("id"), e)}
                    for item in looks_project]
            say("读 OmniFocus 失败（%s）：没法核对这里面有没有项目，先按普通任务导入。"
                % e, "info")
            return list(items or ()), [], []
        by_pk = {}
        for row in rows or ():
            pk = str(row.get("id") or "")
            if pk:
                by_pk[pk] = row
        kept, projects, refused = [], [], []
        for item in items or ():
            row = by_pk.get(str(item.get("id") or ""))
            if not row or row.get("kind") != "project_root":
                kept.append(item)
                continue
            try:
                payload = pj.project_payload(row, rows)
            except ValueError as e:
                say("认不出这个项目（%s）：先按普通任务导入。" % e, "warn")
                kept.append(item)
                continue
            # 项目当前 active 的任务已经在清单里；重复任务 / 被推迟到本周开放的
            # 任务要靠 repetitionInfo + defer 才能看到，统一算进本周账。
            payload = pj.attach_week(payload,
                                     self._project_repeat_infos(payload, say))
            for note in payload.get("warnings") or ():
                say("⚠ %s" % note, "warn")
            if (payload.get("missing") or payload.get("week_missing")
                    or payload.get("depth_errors")):
                refused.append(payload)
                continue
            projects.append(payload)
        return kept, projects, refused
    def cmd_task_page(self, args=None):
        """``open <页码>`` / ``task page <页码>``：直接翻到任务表第 N 页。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks or not re.match(r"^\d+$", toks[0]):
            raise ValueError("用法：open <页码>（例如 open 1 / open 2）")
        want = int(toks[0])
        if want < 1:
            raise ValueError("页码从 1 开始；你给的是 %d" % want)
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        filtered = self.filter_and_sort_tasks(self.task_store.list())
        limit = self.view._row_budget() if isinstance(self.view, TaskView) else 10
        total_pages = max(1, (len(filtered) + limit - 1) // limit)
        if want > total_pages:
            raise ValueError("当前只有 %d 页（共 %d 条任务），没有第 %d 页"
                             % (total_pages, len(filtered), want))
        self.task_view_page = want
        self.say("已翻到第 %d/%d 页（共 %d 条）" % (want, total_pages, len(filtered)), "cmd")
        self.invalidate()
        return want

    def cmd_task_sort(self, args=None):
        """``sort <字段> [asc|desc]`` / ``sort off``：任务表排序。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            if self.task_sort_key:
                self.say("当前排序：%s %s（`sort off` 取消）"
                         % (self.task_sort_key, "降序" if self.task_sort_desc else "升序"),
                         "info")
            else:
                self.say("当前未排序（默认按内部 id 顺序；用法：sort <字段> [asc|desc]）", "info")
            return self.task_sort_key
        first = toks[0].lower()
        if first in ("off", "none", "clear", "reset", "取消", "默认"):
            self.task_sort_key = None
            self.task_sort_desc = False
            self.task_view_page = 1
            self.say("已恢复默认排序", "cmd")
            self.invalidate()
            return None
        desc = False
        key_tok = toks[0]
        if key_tok.startswith("-"):
            desc = True
            key_tok = key_tok[1:]
        elif key_tok.startswith("+"):
            key_tok = key_tok[1:]
        if len(toks) > 1:
            dir_tok = toks[1].lower()
            if dir_tok in ("desc", "down", "reverse", "d", "降序", "倒序"):
                desc = True
            elif dir_tok in ("asc", "up", "a", "升序", "正序"):
                desc = False
            else:
                raise ValueError("排序方向只认 asc / desc；你给的是 %r" % toks[1])
        tasks = self.task_store.list()
        col = normalize_task_column(key_tok, tasks)
        if not col:
            headers = task_table_headers(tasks)
            raise ValueError("sort 只认任务字段或表头名（%s）；你给的是 %r"
                             % ("、".join(headers), key_tok))
        self.task_sort_key = col
        self.task_sort_desc = desc
        self.task_view_page = 1
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        self.say("任务表按「%s」%s排列（`sort off` 恢复默认）"
                 % (col, "降序" if desc else "升序"), "cmd")
        self.invalidate()
        return col

    def cmd_task_group(self, args=None):
        """``group <字段>`` / ``group off``：任务表按字段前置分组显示。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            if self.task_group_by:
                self.say("当前分组：grouping(%s)（`group off` 取消）" % self.task_group_by, "info")
            else:
                self.say("当前未分组（用法：group <字段>，如 `group project`）", "info")
            return self.task_group_by
        raw = " ".join(toks).strip()
        m = re.match(r"^grouping\((.+)\)$", raw, flags=re.I)
        if m:
            raw = m.group(1).strip()
        if raw.lower() in ("off", "none", "clear", "reset", "取消"):
            self.task_group_by = None
            self.task_view_page = 1
            self.say("已取消任务表分组", "cmd")
            self.invalidate()
            return None
        tasks = self.task_store.list()
        col = normalize_task_column(raw, tasks)
        if not col:
            headers = task_table_headers(tasks)
            raise ValueError("group 只认任务字段或表头名（%s）；你给的是 %r"
                             % ("、".join(headers), raw))
        self.task_group_by = col
        self.task_view_page = 1
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        self.say("任务表已按「grouping(%s)」分组显示（同组首行显示，后续留空；`group off` 取消）"
                 % col, "cmd")
        self.invalidate()
        return col

    def cmd_task_search(self, args=None):
        """``search <关键词> [--group <字段>]`` / ``search off``：搜索并可选分组。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            if self.task_search_query or self.task_group_by:
                self.say("当前搜索：%r · 分组：%s（`search off` 清空）"
                         % (self.task_search_query or "（无）",
                            ("grouping(%s)" % self.task_group_by) if self.task_group_by else "（无）"),
                         "info")
            else:
                self.say("用法：search <关键词> [--group <字段>] | search grouping(project) [关键词] | search off",
                         "info")
            return self.task_search_query
        if len(toks) == 1 and toks[0].lower() in ("off", "none", "clear", "reset", "取消"):
            self.task_search_query = ""
            self.task_group_by = None
            self.task_view_page = 1
            self.say("已清除任务搜索与分组过滤", "cmd")
            self.invalidate()
            return ""
        group_arg = None
        query_parts = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            m_grp = re.match(r"^(?:grouping|group)\((.+)\)$", tok, flags=re.I)
            if m_grp:
                group_arg = m_grp.group(1).strip()
                i += 1
                continue
            if low in ("--group", "-g", "by", "--by"):
                if i + 1 >= len(toks):
                    raise ValueError("%s 后面要跟分组字段名（例如 --group project）" % tok)
                group_arg = toks[i + 1]
                i += 2
                continue
            if low.startswith("--group=") or low.startswith("-g="):
                group_arg = tok.split("=", 1)[1].strip()
                i += 1
                continue
            query_parts.append(tok)
            i += 1
        tasks = self.task_store.list()
        # 如果只敲了 `search project` 这类恰好是字段名的单token，而且没带别的词，
        # 既当搜索也可当按该字段分组？按用户说的：「比如说搜索的时候，在最前面引入一个 field，
        # 然后一个这个 field 对应很多 task，比如说 grouping(project)」。
        # 如果第一个词是已知可分组字段名（如 project）且用户写了 `search grouping(project)`
        # 或 `search -g project` 已经识别；若用户写 `search by project` 也能识别。
        if group_arg is not None:
            if group_arg.lower() in ("off", "none", "clear"):
                self.task_group_by = None
            else:
                col = normalize_task_column(group_arg, tasks)
                if not col:
                    raise ValueError("未知分组字段 %r" % group_arg)
                self.task_group_by = col
        self.task_search_query = " ".join(query_parts).strip()
        self.task_view_page = 1
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        matched = self.filter_and_sort_tasks(tasks)
        msg_bits = []
        if self.task_search_query:
            msg_bits.append("搜索「%s」" % self.task_search_query)
        if self.task_group_by:
            msg_bits.append("分组 grouping(%s)" % self.task_group_by)
        self.say("%s：命中 %d/%d 条任务（`search off` 清除）"
                 % (" · ".join(msg_bits) or "全部任务", len(matched), len(tasks)), "cmd")
        self.invalidate()
        return matched
