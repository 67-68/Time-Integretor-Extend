"""warden_core.shell —— WardenShell（画帧缓存、底部预算裁切、PageUp/PageDown 帧内滚动）。"""
import shutil
import time

from .common import (
    ANSI,
    FormattedText,
    repl_kit,
    to_formatted_text,
)

# ---------------------------------------------------------------- 输入（Tab 切视图 / 原地刷新）


class WardenShell(repl_kit.ReplShell):
    """repl_kit 的 shell，加一条只属于看板型 TUI 的让步。

    prompt_toolkit 把 `frame()` 当成**多行 prompt**：内容比终端高时，它先
    保住最下面的输入行，然后从看板**底部**裁掉几行。被裁的恰好是 Recent、
    视图栏和命令提示 —— 这就是「warden 最下面几行被吞掉」的原因。

    这里在交给 prompt_toolkit 之前，先按终端高度把看板裁到「底部预算」内：
    要裁就从**顶部**裁，把最下面的东西（消息 / 视图栏 / 提示行 / 状态行 /
    输入行）永远留在屏幕上。矮终端里看板顶部本来就装不下 —— 与其让它把底部
    挤掉，不如让顶部让位。

    补全菜单的空间由 repl_kit 的 reserve_space_for_menu 决定；warden 把它
    设成 0（菜单改成浮在看板上面），所以这里只需要给它留 0 行。

    还有一件：**画帧要缓存**。prompt_toolkit 每敲一个字就重画一次 chrome
    （`on_text_changed` → `invalidate()`），而 chrome 会把整块看板从 rich 重新
    排版成 ANSI（表格 + 任务对照表 + Recent + 状态框，20ms 上下），再解析回
    FormattedText（又是几毫秒）。按 10 字/秒算，每秒有 250ms 花在「画一模一样的
    东西」上 —— 这就是输入时肉眼可见的掉帧。

    所以这里按「帧键」缓存：键一样就直接复用上次的 ANSI 文本和 fragments。
    看板里唯一自己会动的东西是时钟和「多久以前」，全是秒级，所以同一秒里
    复用一帧看不出差别；状态一变（`invalidate_frame()`）立刻作废。
    """

    def __init__(self, *args, **kwargs):
        pin_bottom = kwargs.pop("pin_bottom", True)
        super().__init__(*args, **kwargs)
        self.pin_bottom = bool(pin_bottom) # 把输入行永远钉在屏幕最底部
        self.frame_rev = 0                 # 状态变化计数：变了就必须重排版
        self.frame_key_hook = None         # (() -> tuple)：业务侧补充的帧键
        self._frame_memo = None            # (key, ansi_text)
        self._ansi_memo = None             # (ansi_text, chunks)
        # 内部滚动：0 = 贴底（最新内容）。向上翻时离开底部，最多翻到整块
        # 看板的第一行；不会把内容漏到终端 scrollback 里。因为深色终端的
        # Alt-Screen 没有「往上滚」的原生操作，否则超高一截就只能丢顶部。
        self.scroll_offset = 0
        self._max_scroll_offset = 0
        self._scroll_view_index = self.view_index

    def switch_view(self, delta=1):
        """切换视图时回到看板底部：新视图不该继承旧视图的滚动位置。"""
        self.scroll_offset = 0
        self._max_scroll_offset = 0
        self._scroll_view_index = getattr(self, "view_index", 0)
        return super().switch_view(delta)

    def scroll_by(self, pages=1):
        """页面内滚动看板。

        ``pages > 0`` = 往上看旧内容，``< 0`` = 往下回到底部。上限由
        :meth:`_build_frame_text` 按实际看板行数算出，不会滚出空白。
        """
        try:
            step = max(1, int(self.dashboard_budget()) - 3)
        except Exception:                             # noqa: BLE001
            step = 10
        old = self.scroll_offset
        target = max(0, int(self.scroll_offset) + int(pages) * step)
        # _max_scroll_offset 是上一帧实际算出的上限；有了它就不让一次
        # PageUp 把 offset 留在一个下一帧必须回钳的荒谬值里。
        if self._max_scroll_offset:
            target = min(target, self._max_scroll_offset)
        self.scroll_offset = target
        self.invalidate_frame()
        return self.scroll_offset != old

    def scroll_to_bottom(self):
        old = self.scroll_offset
        self.scroll_offset = 0
        if old:
            self.invalidate_frame()
        return bool(old)

    def scroll_hint(self):
        """给 footer 的一小段提示；没滚过就不占地方。"""
        if not self.scroll_offset:
            return ""
        total = max(0, self._max_scroll_offset)
        if not total:
            return ""
        return "↕ 已上翻 %d/%d 行 · PgDn 回底部" % (
            min(self.scroll_offset, total), total)

    def key_bindings(self):
        """框架键 + PageUp/PageDown：长看板在**帧内**有限滚动。"""
        bindings = super().key_bindings()

        @bindings.add("pageup", eager=True)
        def _(event):  # noqa: F811 - one binding per shell
            self.scroll_by(1)
            event.current_buffer.cancel_completion()
            event.app.invalidate()

        @bindings.add("pagedown", eager=True)
        def _(event):  # noqa: F811
            self.scroll_by(-1)
            event.current_buffer.cancel_completion()
            event.app.invalidate()

        return bindings

    def invalidate_frame(self):
        """状态变了：下一次 chrome() 必须重新排版（不重排版就是旧画面）。"""
        self.frame_rev += 1
        self._frame_memo = None

    def _frame_clock(self):
        """帧键里的时间位（整秒）。看板上最细的时间粒度就是秒。

        单独抽成一个方法是为了测试能钉住它 —— 不然「同一秒里复用」这种
        断言会随机踩在秒的边界上。
        """
        return int(time.time())

    def frame_key(self):
        """这一帧的「身份」：键相等 = 画出来的东西相等。

        只用**微秒级**的东西算键：`_terminal_rows()` 走 `get_app_or_none()`，
        宽度走 `shutil.get_terminal_size()`。窗口大小一变键就变（prompt_toolkit
        本来也会因为 resize 重画）。
        """
        try:
            width = shutil.get_terminal_size((80, 24)).columns
        except Exception:                             # noqa: BLE001
            width = 80
        key = [self.frame_rev, self.view_index, self._frame_clock(), width,
               self._terminal_rows(), bool(self.show_warnings), bool(self.pin_bottom),
               int(self.scroll_offset)]
        try:
            key.append(self.warnings.signature())
        except Exception:                             # noqa: BLE001
            key.append(object())
        if self.frame_key_hook is not None:
            try:
                key.extend(self.frame_key_hook())
            except Exception:                         # noqa: BLE001 - 键算不出来就别用缓存
                key.append(object())
        return tuple(key)

    def frame_text(self):
        key = self.frame_key()
        memo = self._frame_memo
        if memo is not None and memo[0] == key:
            return memo[1]
        raw = self._build_frame_text()
        # 记的是**画完之后**的键：排版这一趟本身可能改了状态（重读 data.csv、
        # 重算警告、收尾面板刚起步），拿画之前的键去存，下一次就对不上、
        # 白重画一遍（第一帧永远是「画了->又画一遍」）。
        self._frame_memo = (self.frame_key(), raw)
        return raw

    def chrome(self, text=""):
        """父类的 chrome + 「看板没变就别重新解析 ANSI」的缓存。

        父类每次都把整块看板的 ANSI 交给 prompt_toolkit 的解析器（几十行，
        几毫秒）。看板没变时那几毫秒纯属白烧，所以把 fragments 也存下来。
        拼装顺序和父类一致：看板 → 提示行 → 状态行 → 提示符。
        """
        dashboard = self.frame_text()
        chunks = None
        if dashboard:
            memo = self._ansi_memo
            if memo is not None and memo[0] is dashboard:
                chunks = list(memo[1])
            else:
                chunks = self._dashboard_chunks(dashboard)
                self._ansi_memo = (dashboard, tuple(chunks))
        else:
            chunks = []
        # 底部状态栏（视图栏）：框架把它排在打断栏位下面、提示行上面 —— 这里照抄。
        footer = self.footer_text()
        scroll_hint = self.scroll_hint()
        if scroll_hint:
            # 只在上翻后追加，平常不占额外宽度。footer 本来就是一行。
            footer = ((footer.rstrip("\n") + "    " + scroll_hint + "\n")
                      if footer else (scroll_hint + "\n"))
        if footer:
            chunks.extend(self._dashboard_chunks(footer))
        hint = self.hint_line(text)
        if hint:
            chunks.append((self.pt_hint_class, hint + "\n"))
        status = self.status_line()
        if status:
            chunks.append((self.pt_status_class, status + "\n"))
        chunks.append((self.pt_prompt_class, self.prompt))
        return FormattedText(chunks) if FormattedText is not None else chunks

    @staticmethod
    def _dashboard_chunks(dashboard):
        """ANSI 文本 → prompt_toolkit 的 fragments（和父类同一套规则）。"""
        if "\x1b" in dashboard and ANSI is not None:
            return list(to_formatted_text(ANSI(dashboard)))
        return [("", dashboard)]

    def dashboard_budget(self):
        """看板画布的可用高度（终端总行数 - 底部视图栏/提示行/状态行/输入行）。"""
        reserved = 1                                  # 提示行
        # 老版本 repl_kit 没有这个属性时，PromptSession 仍会预留 8 行。
        reserved += max(0, getattr(self, "reserve_space_for_menu", 8))
        try:
            has_status = bool(self.status_line())
        except Exception:                             # 状态行坏了也不该撑破布局
            has_status = False
        if has_status:
            reserved += 1                             # 状态行（专注视图）
        try:
            has_footer = bool(self.footer_text())
        except Exception:                             # 视图栏坏了也别撑破布局
            has_footer = False
        if has_footer:
            reserved += 1                             # 底部视图栏
        reserved += 1                                 # 提示符
        return max(1, self._terminal_rows() - reserved)

    def _build_frame_text(self):
        """真正排版一次，并把看板裁进「一屏固定高度」的视口。

        不再让 prompt_toolkit 自己处理超高内容：那会从底部裁、还会把内容
        推进终端 scrollback。这里从多行看板里取一个窗口：

        * ``scroll_offset == 0``：永远显示最下面一屏（输入/消息/表格底部）；
        * 按 PageUp：窗口整体上移，最多到第一行；PageDown 回到底部；
        * 内容不足一屏：补空行把输入框钉在底部。
        """
        self._in_frame_text = True
        try:
            raw = super().frame_text()
        finally:
            self._in_frame_text = False
        if not raw:
            return raw
        budget = max(1, self.dashboard_budget())
        lines = raw.splitlines()
        # 视图切换可能发生在框架内部（set_view 走 switch_view，已重置）；
        # 这里再兜一层，防止别的调用直接改 view_index。
        if self.view_index != self._scroll_view_index:
            self.scroll_offset = 0
            self._scroll_view_index = self.view_index
        max_offset = max(0, len(lines) - budget)
        self._max_scroll_offset = max_offset
        if self.scroll_offset > max_offset:
            self.scroll_offset = max_offset
        if self.scroll_offset < 0:
            self.scroll_offset = 0
        if len(lines) > budget:
            end = max(0, len(lines) - self.scroll_offset)
            start = max(0, end - budget)
            visible = lines[start:end]
            if self.scroll_offset > 0 and budget >= 3:
                # 顶部用一行说明当前视口位置；其余仍显示原内容。这个提示
                # 本身也算在预算内，所以总高度绝不会超过终端。
                shown = min(self.scroll_offset, max_offset)
                indicator = "↕ 上翻 %d/%d 行 · PgUp 更早 · PgDn 回底部" % (
                    shown, max_offset)
                keep = budget - 1
                start = max(0, len(lines) - budget - self.scroll_offset)
                visible = [indicator] + lines[start:start + keep]
            raw = "\n".join(visible) + "\n"
        elif self.pin_bottom and len(lines) < budget:
            # 内容不足一屏时在底部补齐空行，让视图栏/提示行/输入行永远钉在屏幕最底部。
            pad = budget - len(lines)
            raw = raw.rstrip("\n") + ("\n" * (pad + 1))
        return raw

    def print(self, *objects, **kwargs):
        """全屏 App 内的标准输出改走帧内消息，不写终端历史。

        ``ReplShell.dispatch`` 的 unknown / parse-error / exception 分支会直接
        调 ``print``；在常驻 full-screen Application 里那会把文字写在渲染器
        背后（就是用户看到的「拉一下终端就多出一截历史」）。Warden 把这些
        输出接成 Recent 消息；--exec / 测试 / 普通模式仍按原样输出。
        """
        hook = getattr(self, "output_hook", None)
        if getattr(self, "_app_running", False) and hook is not None:
            try:
                hook(*objects, **kwargs)
            except Exception:                         # noqa: BLE001 - 别因为报错再报错
                pass
            # 全屏时**绝不能**回落到 print：那正是写穿渲染器、留下
            # scrollback 的路径。接不住宁可丢一条消息。
            return
        return super().print(*objects, **kwargs)

    @staticmethod
    def _terminal_rows():
        """这次渲染实际可用的行数。

        优先问 prompt_toolkit 的 output（离屏渲染/测试时它才是真的）；
        拿不到再退回终端尺寸。两者在真 TTY 下是同一个数。

        这里**不能**用 `get_app()`：没有正在跑的 Application 时它会现造一个
        DummyApplication（好几毫秒，还要 load 一遍按键表），而这个函数每画一帧
        都要调一次 —— 以前打字掉帧有一部分就是它。
        """
        try:
            from prompt_toolkit.application.current import get_app_or_none
        except ImportError:                           # pragma: no cover - 老版本
            get_app_or_none = None
        if get_app_or_none is not None:
            try:
                app = get_app_or_none()
                if app is not None:
                    return app.output.get_size().rows
            except Exception:                         # noqa: BLE001
                pass
        return shutil.get_terminal_size((80, 24)).lines
