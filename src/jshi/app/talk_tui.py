"""Textual 对话壳。仅由 cli 在确认已安装后导入。"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input, Log, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from jshi.app.talk_session import TALK_COMMANDS, TalkOutcome, TalkSession

_NEEDS_ARGUMENT = frozenset({"/speaker"})
_HELP_TOKENS = frozenset({"/", "/help", "/?"})


class _CommandPicker(OptionList):
    """不抢焦点，由输入框的上下键移动高亮。"""

    can_focus = False


class TalkApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }
    #chat {
        height: 1fr;
        border: solid $accent;
        text-wrap: wrap;
    }
    #picker {
        height: auto;
        max-height: 9;
        border: solid $accent;
    }
    #status {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """
    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", show=False),
        Binding("up", "picker_up", "上一项", show=False, priority=True),
        Binding("down", "picker_down", "下一项", show=False, priority=True),
        Binding("escape", "picker_hide", "关闭列表", show=False, priority=True),
    ]

    def __init__(self, session: TalkSession) -> None:
        super().__init__()
        self.session = session
        self._pending = False
        self._picker_moved = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Log(id="chat", wrap=True)
        yield _CommandPicker(id="picker")
        yield Static(self._idle_status(), id="status")
        yield Input(placeholder="说话；输入 / 用箭头选命令", id="line")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "匠石"
        self.sub_title = f"{self.session.subject_id} · {self.session.speaker}"
        chat = self.query_one("#chat", Log)
        chat.write_line("直接打字后回车即发送。输入 / 或 /help，上下箭头选择命令。")
        self._picker().display = False
        self.query_one("#line", Input).focus()

    def _idle_status(self) -> str:
        return f"主体 {self.session.subject_id}；对象 {self.session.speaker}（长期）"

    def _picker(self) -> _CommandPicker:
        return self.query_one("#picker", _CommandPicker)

    def _picker_visible(self) -> bool:
        return bool(self._picker().display)

    def action_picker_up(self) -> None:
        if self._picker_visible():
            self._picker().action_cursor_up()
            self._picker_moved = True
            return
        self.query_one("#chat", Log).scroll_relative(y=-1)

    def action_picker_down(self) -> None:
        if self._picker_visible():
            self._picker().action_cursor_down()
            self._picker_moved = True
            return
        self.query_one("#chat", Log).scroll_relative(y=1)

    def action_picker_hide(self) -> None:
        if not self._picker_visible():
            return
        self._hide_picker()
        field = self.query_one("#line", Input)
        if field.value.strip() in _HELP_TOKENS or (
            field.value.startswith("/") and " " not in field.value.strip()
        ):
            field.value = ""
        field.focus()

    def _hide_picker(self) -> None:
        picker = self._picker()
        picker.display = False
        picker.clear_options()
        self._picker_moved = False
        if not self._pending:
            self.query_one("#status", Static).update(self._idle_status())

    def _matching_commands(self, typed: str) -> tuple[tuple[str, str, str], ...]:
        stripped = typed.strip()
        if not stripped.startswith("/") or " " in stripped:
            return ()
        if stripped in _HELP_TOKENS:
            return TALK_COMMANDS
        return tuple(
            item for item in TALK_COMMANDS if item[0].startswith(stripped)
        )

    def _sync_picker(self, typed: str) -> None:
        matches = self._matching_commands(typed)
        picker = self._picker()
        picker.clear_options()
        self._picker_moved = False
        if not matches:
            picker.display = False
            if not self._pending:
                self.query_one("#status", Static).update(self._idle_status())
            return
        for name, argument, summary in matches:
            usage = f"{name} {argument}".strip() if argument else name
            picker.add_option(Option(f"{usage:<16} {summary}", id=name))
        picker.highlighted = 0
        picker.display = True
        status = self.query_one("#status", Static)
        status.update("↑↓ 选择命令，回车执行，Esc 关闭")

    def _highlighted_name(self) -> str | None:
        picker = self._picker()
        option = picker.highlighted_option
        if option is None:
            return None
        return option.id

    def on_input_changed(self, event: Input.Changed) -> None:
        self._sync_picker(event.value)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        name = event.option.id
        if not name:
            return
        self.query_one("#line", Input).value = ""
        self._hide_picker()
        self._execute_picked(name)

    def _execute_picked(self, name: str) -> None:
        if name in _NEEDS_ARGUMENT:
            field = self.query_one("#line", Input)
            field.value = f"{name} "
            field.focus()
            return
        self.query_one("#chat", Log).write_line(f"你：{name}")
        self._apply(self.session.handle(name))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        if self._picker_visible() and self._use_highlight(line):
            name = self._highlighted_name()
            event.input.value = ""
            self._hide_picker()
            if name:
                self._execute_picked(name)
            else:
                event.input.focus()
            return
        event.input.value = ""
        if not line:
            return
        if self._pending or self.session.busy:
            self.query_one("#status", Static).update("上一轮尚未结束，请稍候。")
            return
        if line in _HELP_TOKENS:
            self._sync_picker("/")
            event.input.value = "/help"
            event.input.focus()
            return
        self.query_one("#chat", Log).write_line(f"你：{line}")
        if line.startswith("/"):
            self._apply(self.session.handle(line))
            return
        self._pending = True
        self.query_one("#status", Static).update("等待回应…")
        self.run_worker(
            lambda: self.session.handle(line),
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def _use_highlight(self, stripped: str) -> bool:
        if not self._picker_visible():
            return False
        if stripped in _HELP_TOKENS:
            return self._picker_moved
        if stripped.startswith("/") and " " not in stripped:
            exact = {name for name, _argument, _summary in TALK_COMMANDS}
            if stripped in exact:
                return False
            return True
        return False

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            self._pending = False
            self.query_one("#status", Static).update("调用失败。")
            return
        if event.state is not WorkerState.SUCCESS:
            return
        outcome = event.worker.result
        if not isinstance(outcome, TalkOutcome):
            self._pending = False
            return
        self._apply(outcome)

    def _apply(self, outcome: TalkOutcome) -> None:
        self._pending = False
        chat = self.query_one("#chat", Log)
        status = self.query_one("#status", Static)
        meta_text = None
        for event in outcome.events:
            if event.kind == "speech":
                chat.write_line(f"匠石：{event.text}")
            elif event.kind in {"notice", "overlay"}:
                for line in event.text.splitlines() or ("",):
                    chat.write_line(line)
            elif event.kind == "meta":
                meta_text = event.text
        if meta_text:
            status.update(meta_text)
        else:
            status.update(self._idle_status())
        self.sub_title = f"{self.session.subject_id} · {self.session.speaker}"
        self.query_one("#line", Input).focus()
        if outcome.quit:
            self.exit()


def run_tui(session: TalkSession) -> None:
    TalkApp(session).run()
