"""Main agent observe→act loop with parallel_delegate support."""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from ..browser.session import BrowserSession
from ..config import AgentConfig
from ..llm import LLM, Usage, text_of
from .console import AgentConsole
from .context import Conversation
from .prompts import SYSTEM
from .safety import SafetyPolicy
from .tools import Toolbox
from .trace import Trace
from .workers import merge_worker_reports, run_parallel_workers, run_reading_helper

STALL_NUDGE = """<system-reminder>
You have repeated the same action {n} times with no useful change. Stop and
re-plan before acting again: say what you expected, what actually happened, and
choose a different route. If independent sub-goals remain, consider
parallel_delegate.
</system-reminder>"""

FAILURE_NUDGE = """<system-reminder>
{n} consecutive tool calls failed. Take a fresh browser_snapshot before doing
anything else - your handles are probably stale, or the page is not what you
think it is.
</system-reminder>"""

FINISH_NUDGE = """<system-reminder>
You ended your turn without calling a tool. If the task is done, call `finish`
with a report for the user. If it is not, continue working. If you are blocked
on something only the user can supply, call `ask_user`.
</system-reminder>"""

BUDGET_NUDGE = """<system-reminder>
Only {n} steps remain in this run. Wrap up: secure what you have achieved and
call `finish` with an honest report of what is done and what is not.
</system-reminder>"""


@dataclass
class RunResult:
    status: str
    report: str
    steps: int
    usage: Usage
    notes: dict[str, str] = field(default_factory=dict)


class AgentLoop:
    def __init__(
        self,
        cfg: AgentConfig,
        session: BrowserSession,
        console: AgentConsole,
        trace: Trace | None = None,
    ) -> None:
        self.cfg = cfg
        self.session = session
        self.console = console
        self.trace = trace
        self.usage = Usage()
        self.llm = LLM(cfg.model, self.usage)
        self.convo = Conversation(cfg=cfg.context, llm=self.llm)
        self.safety = SafetyPolicy(
            cfg=cfg.safety,
            llm=self.llm if cfg.safety.use_llm_judge else None,
            confirm=self._confirm,
        )
        self.tools = Toolbox(
            session,
            cfg,
            self.safety,
            ask_user=self._ask_user,
            delegate_reading=self._delegate_reading,
            parallel_delegate=self._parallel_delegate,
            notes=self.convo.notes,
            readonly=False,
            role="main",
        )
        self._recent_calls: deque[str] = deque(maxlen=6)
        self._consecutive_failures = 0
        self._storage_state_path: Path | None = None

    def _confirm(self, tool: str, args: dict[str, Any], verdict: Any) -> bool:
        answer = self.console.confirm_action(tool, args, verdict)
        if answer == "a":
            self.safety.always_allow.add(f"{tool}|{self.tools.label(args.get('ref'))}")
        allowed = answer in ("y", "a")
        if self.trace:
            self.trace.write(
                "gate",
                tool=tool,
                args=args,
                risk=verdict.risk,
                reason=verdict.reason,
                allowed=allowed,
            )
        return allowed

    def _ask_user(self, question: str) -> str:
        answer = self.console.ask_user(question)
        if self.trace:
            self.trace.write("ask_user", question=question, answer=answer)
        return answer

    def _delegate_reading(self, instruction: str, max_steps: int) -> str:
        return run_reading_helper(
            self.llm,
            self.cfg,
            self.session,
            self.console,
            instruction,
            max_steps,
            self.trace,
        )

    def _parallel_delegate(self, tasks: list[dict[str, Any]]) -> str:
        """Export cookies from the main session, then fan out action workers."""
        if self.cfg.workers.inherit_session:
            state_path = (self.trace.dir if self.trace else self.cfg.runs_dir) / "storage_state.json"
            try:
                self.session.export_storage_state(state_path)
                self._storage_state_path = state_path
                self.console.note(f"exported storage_state → {state_path}")
            except Exception as exc:  # noqa: BLE001
                self.console.warn(f"could not export storage_state: {exc}")
                state_path = None
        else:
            state_path = None

        if self.trace:
            self.trace.write("parallel_delegate", tasks=tasks)

        results = run_parallel_workers(
            tasks=tasks,
            storage_state_path=state_path,
            cfg=self.cfg,
            console=self.console,
            trace=self.trace,
            run_dir=(self.trace.dir / "workers") if self.trace else self.cfg.runs_dir / "workers",
        )
        return merge_worker_reports(results)

    def system_blocks(self) -> Any:
        if self.cfg.model.profile != "anthropic":
            return SYSTEM
        return [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]

    def run(self, task: str) -> RunResult:
        page = self.session.active_page()
        self.convo.add_user_text(
            f'<environment browser="chromium" url="{page.url}" tabs="{len(self.session.pages)}"/>\n'
            f"<task>\n{task}\n</task>"
        )
        if self.trace:
            self.trace.write("task", task=task, url=page.url, model=self.cfg.model.main)

        tools = self.tools.schemas(readonly=False, role="main")
        system = self.system_blocks()
        nudges = 0

        for step in range(1, self.cfg.max_steps + 1):
            tokens = self.convo.tokens(system, tools)
            self.console.step(step, tokens, self.cfg.context.budget_tokens)
            self._manage_context(task, tokens)

            if step == self.cfg.max_steps - 2:
                self.convo.add_user_text(BUDGET_NUDGE.format(n=3))

            try:
                response = self._call_model(system, tools)
            except Exception as exc:  # noqa: BLE001
                self.console.error(f"model call failed: {exc}")
                if self.trace:
                    self.trace.write("model_error", error=str(exc))
                return RunResult(
                    "failed",
                    "The run stopped because the model was unreachable: "
                    f"{type(exc).__name__}: {exc}\n\n"
                    f"Progress so far: {json.dumps(self.convo.notes, ensure_ascii=False)}",
                    step,
                    self.usage,
                    dict(self.convo.notes),
                )

            if getattr(response, "stop_reason", None) == "refusal":
                return RunResult(
                    "failed",
                    "The model declined to continue with this task.",
                    step,
                    self.usage,
                    dict(self.convo.notes),
                )

            self.convo.add_assistant(response.content)
            self._render_assistant(response.content)

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                nudges += 1
                if nudges > 2:
                    return RunResult(
                        "partial",
                        text_of(response.content) or "(no report)",
                        step,
                        self.usage,
                        dict(self.convo.notes),
                    )
                self.convo.add_user_text(FINISH_NUDGE)
                continue
            nudges = 0

            results, terminal = self._execute(tool_uses, step)
            if terminal is not None:
                return RunResult(
                    terminal[1], terminal[0], step, self.usage, dict(self.convo.notes)
                )

            self.convo.add_tool_results(results)
            self._inject_nudges(tool_uses)

        return RunResult(
            "partial",
            "Step budget exhausted before the task was finished. "
            f"Notes gathered: {json.dumps(self.convo.notes, ensure_ascii=False)}",
            self.cfg.max_steps,
            self.usage,
            dict(self.convo.notes),
        )

    def _call_model(self, system: Any, tools: list[dict[str, Any]]) -> Any:
        delay = 20.0
        for attempt in range(4):
            try:
                return self.llm.create(
                    system=system, messages=self.convo.messages, tools=tools
                )
            except anthropic.RateLimitError as exc:
                if attempt == 3:
                    raise
                wait = float(
                    getattr(getattr(exc, "response", None), "headers", {}).get("retry-after", 0)
                ) or delay
                self.console.warn(
                    f"rate limited by the provider — waiting {wait:.0f}s and retrying"
                )
                if self.trace:
                    self.trace.write("rate_limited", attempt=attempt + 1, wait=wait)
                time.sleep(wait)
                delay *= 2
        raise RuntimeError("unreachable")

    def _manage_context(self, task: str, tokens: int) -> None:
        pruned = self.convo.prune()
        if pruned:
            self.console.note(f"pruned {pruned} stale page observation(s) from context")
        if self.convo.needs_compaction(tokens):
            self.console.note(f"context at {tokens:,} tokens — compacting the transcript")
            url = self.session.active_page().url
            summary = self.convo.compact(task, url)
            if summary:
                after = self.convo.tokens(self.system_blocks(), self.tools.schemas())
                self.console.note(f"compacted to ~{after:,} tokens")
                if self.trace:
                    self.trace.write("compaction", before=tokens, after=after, summary=summary)

    def _render_assistant(self, content: Any) -> None:
        for block in content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                self.console.thinking(block.thinking)
            elif block.type == "text":
                self.console.assistant(block.text)

    def _execute(
        self, tool_uses: list[Any], step: int
    ) -> tuple[list[dict], tuple[str, str] | None]:
        results: list[dict[str, Any]] = []
        for call in tool_uses:
            args = dict(call.input)
            self.console.tool_call(call.name, args)
            if self.trace:
                self.trace.write("tool_call", step=step, tool=call.name, args=args)

            outcome = self.tools.dispatch(call.name, args)

            if outcome.terminal:
                return results, (outcome.report or "", outcome.status or "completed")

            preview = outcome.content if isinstance(outcome.content, str) else "<non-text>"
            self.console.tool_result(preview, outcome.is_error)
            if self.trace:
                self.trace.write(
                    "tool_result",
                    step=step,
                    tool=call.name,
                    error=outcome.is_error,
                    content=preview[:4000],
                    video=self.session.video_path(),
                )

            self._consecutive_failures = (
                self._consecutive_failures + 1 if outcome.is_error else 0
            )
            self._recent_calls.append(
                f"{call.name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
            )

            content = outcome.content
            if isinstance(content, str) and len(content) > self.cfg.context.tool_result_chars:
                content = content[: self.cfg.context.tool_result_chars] + "\n…[result truncated]"
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": content,
                    "is_error": outcome.is_error,
                }
            )
            if outcome.observation:
                self.convo.mark_observation(call.id, outcome.observation, step)
        return results, None

    def _inject_nudges(self, tool_uses: list[Any]) -> None:
        if self._consecutive_failures >= 3:
            self.console.warn("three failures in a row — forcing a re-observation")
            self.convo.add_user_text(FAILURE_NUDGE.format(n=self._consecutive_failures))
            self._consecutive_failures = 0
            return
        if len(self._recent_calls) >= 3:
            last = self._recent_calls[-1]
            repeats = sum(1 for c in self._recent_calls if c == last)
            if repeats >= 3:
                self.console.warn(f"the same call was made {repeats} times — forcing a re-plan")
                self.convo.add_user_text(STALL_NUDGE.format(n=repeats))
                self._recent_calls.clear()
