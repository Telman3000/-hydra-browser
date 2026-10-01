"""Action-capable parallel workers — each owns an isolated Chromium (sync Playwright).

Playwright's sync API is not thread-safe across a shared instance, so every worker
starts its own sync_playwright() + chromium.launch() in its thread. Workers are
NOT read-only: they click, type and navigate. Cookies come from a storage_state
export of the orchestrator's persistent profile.
"""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from ..browser.layout import viewport_for, window_args, worker_window_rect
from ..browser.session import BrowserSession, _INIT_SCRIPT, resolve_browser_channel
from ..config import AgentConfig, SafetyConfig
from ..llm import LLM, Usage, text_of
from .console import AgentConsole
from .prompts import READER_SYSTEM, WORKER_SYSTEM
from .safety import SafetyPolicy, Verdict
from .tools import Toolbox
from .trace import Trace

log = logging.getLogger(__name__)

WORKER_PREFIX = "   [dim]│ w{wid}[/dim] "


@dataclass
class WorkerResult:
    worker_id: int
    status: str
    report: str
    steps: int = 0
    error: str | None = None


@dataclass
class _EphemeralSession:
    """Thin adapter: a non-persistent Chromium context that looks like BrowserSession."""

    headless: bool = False
    slow_mo_ms: int = 0
    storage_state_path: Path | None = None
    default_timeout_ms: int = 12_000
    video_dir: Path | None = None
    slot: int = 0
    slots: int = 1
    layout: str = "default"

    _pw: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)
    _context: Any = field(default=None, init=False, repr=False)
    page: Any = field(default=None, init=False, repr=False)
    pending_dialogs: list = field(default_factory=list, init=False)
    last_console_errors: list = field(default_factory=list, init=False)
    on_page_event: Any = None
    dialog_policy: str = "accept"

    def start(self, start_url: str | None = None) -> None:
        from ..browser.session import resolve_browser_channel

        self._pw = sync_playwright().start()
        rect = worker_window_rect(self.layout, self.slot, self.slots)
        try:
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "slow_mo": self.slow_mo_ms,
                "args": window_args(rect),
                "ignore_default_args": ["--enable-automation"],
                "chromium_sandbox": True,
            }
            channel = resolve_browser_channel(self._pw, headless=self.headless)
            if channel:
                launch_kwargs["channel"] = channel
            self._browser = self._pw.chromium.launch(**launch_kwargs)
            ctx_kwargs: dict[str, Any] = {"no_viewport": True}
            if self.video_dir:
                Path(self.video_dir).mkdir(parents=True, exist_ok=True)
                viewport = viewport_for(rect if self.layout == "split" else None, (1280, 800))
                ctx_kwargs.update(
                    no_viewport=False,
                    viewport=viewport,
                    record_video_dir=str(self.video_dir),
                    record_video_size=viewport,
                )
            if self.storage_state_path and Path(self.storage_state_path).exists():
                ctx_kwargs["storage_state"] = str(self.storage_state_path)
            self._context = self._browser.new_context(**ctx_kwargs)
            self._context.set_default_timeout(self.default_timeout_ms)
            self._context.add_init_script(path=str(_INIT_SCRIPT))
            self._context.on("page", self._on_new_page)
            self.page = self._context.new_page()
            self._wire_page(self.page)
            # Windows opened by a background process may land behind others.
            self.page.bring_to_front()
            if start_url:
                self.goto(start_url)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # --- BrowserSession-compatible surface used by actions/tools ---

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("worker browser not started")
        return self._context

    @property
    def pages(self):
        return [p for p in self.context.pages if not p.is_closed()]

    def video_path(self) -> str | None:
        page = self.page
        if page is None or page.video is None:
            return None
        try:
            return page.video.path()
        except Exception:  # noqa: BLE001
            return None

    def active_page(self):
        if self.page is None or self.page.is_closed():
            self.page = self.pages[-1] if self.pages else self.context.new_page()
            self._wire_page(self.page)
        return self.page

    def select_tab(self, index: int):
        pages = self.pages
        if not 0 <= index < len(pages):
            raise IndexError(f"tab {index} does not exist")
        self.page = pages[index]
        self.page.bring_to_front()
        return self.page

    def new_tab(self, url: str | None = None):
        page = self.context.new_page()
        self._wire_page(page)
        self.page = page
        if url:
            self.goto(url)
        return page

    def close_tab(self, index: int) -> None:
        pages = self.pages
        if not 0 <= index < len(pages):
            raise IndexError(f"tab {index} does not exist")
        pages[index].close()
        live = [p for p in self.pages if not p.is_closed()]
        if live and (self.page is None or self.page.is_closed()):
            self.page = live[-1]

    def tab_list(self) -> list[dict[str, Any]]:
        active = self.active_page()
        out = []
        for i, p in enumerate(self.pages):
            try:
                out.append(
                    {"index": i, "title": p.title()[:80], "url": p.url, "active": p is active}
                )
            except Exception:  # noqa: BLE001
                out.append({"index": i, "title": "<unavailable>", "url": "", "active": False})
        return out

    def goto(self, url: str, timeout_ms: int | None = None) -> None:
        import re
        import time

        from playwright.sync_api import Error as PlaywrightError

        from ..browser.session import _is_redirect_interruption

        page = self.active_page()
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", url):
            url = "https://" + url
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms or self.default_timeout_ms,
            )
        except PlaywrightError as exc:
            if not _is_redirect_interruption(exc):
                raise
        self.settle()

    def settle(self, quiet_ms: int = 350, timeout_ms: int = 4_000) -> None:
        import time

        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        page = self.active_page()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except (PlaywrightTimeout, PlaywrightError):
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except (PlaywrightTimeout, PlaywrightError):
            pass
        time.sleep(quiet_ms / 1000)

    def frames(self):
        from playwright.sync_api import Error as PlaywrightError

        page = self.active_page()
        out = []
        for f in page.frames:
            try:
                if f.is_detached():
                    continue
                out.append(f)
            except PlaywrightError:
                continue
        return out

    def drain_dialogs(self):
        events, self.pending_dialogs = self.pending_dialogs, []
        return events

    def _wire_page(self, page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("console", self._on_console)

    def _on_new_page(self, page) -> None:
        self._wire_page(page)
        self.page = page

    def _on_dialog(self, dialog) -> None:
        from ..browser.session import DialogEvent

        action = self.dialog_policy
        try:
            if action == "accept":
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:  # noqa: BLE001
            action = "already-handled"
        self.pending_dialogs.append(
            DialogEvent(kind=dialog.type, message=dialog.message[:300], handled_as=action)
        )

    def _on_console(self, msg) -> None:
        if msg.type == "error":
            self.last_console_errors.append(msg.text[:200])
            del self.last_console_errors[:-10]


def _worker_confirm(tool: str, args: dict[str, Any], verdict: Verdict) -> bool:
    """Workers have no human: auto-refuse medium/high risk, allow low."""
    if verdict.rank >= 2:
        return False
    return True


def _salvage_report(llm: LLM, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
    """Out of steps without a report: one last call to write up what was found."""
    if messages and messages[-1]["role"] == "assistant":
        # Its tool calls were never answered; a dangling tool_use is rejected by the API.
        messages = messages[:-1]
    ask = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "You are out of steps. Call finish now with everything you found "
                "so far, and say what is missing.",
            }
        ],
    }
    try:
        response = llm.create(
            system=WORKER_SYSTEM, messages=[*messages, ask], tools=tools, max_tokens=2_000
        )
    except Exception:  # noqa: BLE001
        return ""
    for block in response.content:
        if block.type == "tool_use" and block.name == "finish":
            return str(dict(block.input).get("report") or "")
    return text_of(response.content)


def run_worker_agent(
    goal: str,
    start_url: str | None,
    storage_state_path: Path | str | None,
    cfg: AgentConfig,
    run_dir: Path,
    worker_id: int,
    console: AgentConsole | None = None,
    trace: Trace | None = None,
    slots: int = 1,
) -> WorkerResult:
    """Run an action-capable worker in the calling thread with its own Playwright."""
    usage = Usage()
    llm = LLM(cfg.model, usage)
    session = _EphemeralSession(
        headless=cfg.browser.headless,
        slow_mo_ms=cfg.browser.slow_mo_ms,
        storage_state_path=Path(storage_state_path) if storage_state_path else None,
        video_dir=(run_dir / "video" / f"w{worker_id}") if cfg.browser.record_video else None,
        slot=(worker_id - 1) % max(slots, 1),
        slots=slots,
        layout=cfg.browser.layout,
    )
    prefix = WORKER_PREFIX.format(wid=worker_id)
    # Workers have no human: ask-mode + callback that auto-refuses medium/high.
    safety_cfg = SafetyConfig(mode="ask", use_llm_judge=cfg.safety.use_llm_judge)
    safety = SafetyPolicy(
        cfg=safety_cfg,
        llm=llm if cfg.safety.use_llm_judge else None,
        confirm=_worker_confirm,
    )
    notes: dict[str, str] = {}
    box: Toolbox | None = None
    steps = 0

    try:
        session.start()
        if trace and session.video_path():
            trace.write("worker_video", worker_id=worker_id, video=session.video_path())
        start_note = ""
        if start_url:
            try:
                session.goto(start_url, timeout_ms=30_000)
            except Exception as exc:  # noqa: BLE001
                start_note = (
                    f"\nOpening {start_url} failed ({str(exc).splitlines()[0]}). "
                    "Retry with browser_navigate or work around it."
                )
        box = Toolbox(
            session,  # type: ignore[arg-type]
            cfg,
            safety,
            notes=notes,
            readonly=False,
            role="worker",
        )
        tools = box.schemas(readonly=False, role="worker")
        page = session.active_page()
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f'<worker id="{worker_id}" url="{page.url}"/>\n'
                            f"<goal>\n{goal}\n</goal>{start_note}"
                        ),
                    }
                ],
            }
        ]

        max_steps = cfg.workers.max_steps
        report = ""
        status = "partial"

        for step in range(1, max_steps + 1):
            steps = step
            response = llm.create(
                system=WORKER_SYSTEM,
                messages=messages,
                tools=tools,
                max_tokens=4_000,
                effort="medium",
            )
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            report = text_of(response.content) or report

            if not tool_uses:
                # A full answer given as plain text instead of via finish still counts.
                status = "completed" if len(report.strip()) >= 200 else "partial"
                break

            results = []
            finished = False
            for call in tool_uses:
                args = dict(call.input)
                if console:
                    console.tool_call(call.name, args, prefix=prefix)
                if trace:
                    trace.write("worker_tool", worker_id=worker_id, tool=call.name, args=args)
                outcome = box.dispatch(call.name, args)
                if console and not outcome.terminal:
                    console.tool_result(
                        outcome.content if isinstance(outcome.content, str) else "<non-text>",
                        outcome.is_error,
                        prefix=prefix,
                        lines=1,
                    )
                if outcome.terminal:
                    report = outcome.report or report
                    status = outcome.status or "completed"
                    finished = True
                    break
                content = outcome.content
                if isinstance(content, str) and len(content) > cfg.context.tool_result_chars:
                    content = content[: cfg.context.tool_result_chars] + "\n…[truncated]"
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": content,
                        "is_error": outcome.is_error,
                    }
                )
            if finished:
                break
            messages.append({"role": "user", "content": results})

            if step == max_steps - 1:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Step budget nearly spent. Call finish now with what you have.",
                            }
                        ],
                    }
                )

        if not report.strip():
            report = _salvage_report(llm, messages, tools)
        return WorkerResult(
            worker_id=worker_id,
            status=status,
            report=report or "(worker returned no report)",
            steps=steps,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("worker %s failed", worker_id)
        return WorkerResult(
            worker_id=worker_id,
            status="failed",
            report="",
            steps=steps,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}",
        )
    finally:
        session.close()


def run_parallel_workers(
    tasks: list[dict[str, Any]],
    storage_state_path: Path | str | None,
    cfg: AgentConfig,
    console: AgentConsole,
    trace: Trace | None = None,
    run_dir: Path | None = None,
) -> list[WorkerResult]:
    """Fan out action-capable workers via ThreadPoolExecutor (one Playwright each)."""
    run_dir = run_dir or (cfg.runs_dir / "workers")
    run_dir.mkdir(parents=True, exist_ok=True)
    max_workers = min(len(tasks), cfg.workers.max_workers)
    console.note(f"spawning {len(tasks)} action worker(s) (max_workers={max_workers})")

    results: list[WorkerResult] = []

    def _job(idx: int, task: dict[str, Any]) -> WorkerResult:
        goal = str(task.get("goal") or "")
        start_url = task.get("start_url")
        if trace:
            trace.write("worker_start", worker_id=idx, goal=goal, start_url=start_url)
        console.console.print(
            f"{WORKER_PREFIX.format(wid=idx)}[bold blue]worker[/] {goal[:110]}"
        )
        result = run_worker_agent(
            goal=goal,
            start_url=start_url,
            storage_state_path=storage_state_path,
            cfg=cfg,
            run_dir=run_dir,
            worker_id=idx,
            console=console,
            trace=trace,
            slots=max_workers,
        )
        if trace:
            trace.write(
                "worker_done",
                worker_id=idx,
                status=result.status,
                report=(result.report or "")[:2000],
                error=result.error,
                steps=result.steps,
            )
        console.console.print(
            f"{WORKER_PREFIX.format(wid=idx)}[blue]↩ {result.status} "
            f"({result.steps} steps, {len(result.report or '')} chars)[/blue]"
        )
        return result

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hydra-w") as pool:
        futures = {pool.submit(_job, i + 1, t): i + 1 for i, t in enumerate(tasks)}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: r.worker_id)
    return results


def merge_worker_reports(results: list[WorkerResult]) -> str:
    parts = []
    for r in results:
        block = [
            f"### worker {r.worker_id} — {r.status} ({r.steps} steps)",
            r.report or "(empty)",
        ]
        if r.error:
            block.append(f"error: {r.error}")
        parts.append("\n".join(block))
    return "\n\n".join(parts) if parts else "(no workers ran)"


def run_reading_helper(
    llm: LLM,
    cfg: AgentConfig,
    session: BrowserSession,
    console: AgentConsole,
    instruction: str,
    max_steps: int,
    trace: Trace | None = None,
) -> str:
    """Optional read-only helper sharing the main browser session."""
    safety = SafetyPolicy(cfg=cfg.safety, llm=None, confirm=None)
    box = Toolbox(session, cfg, safety, readonly=True, role="reader")
    tools = box.schemas(readonly=True, role="reader")
    page = session.active_page()
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f'<current_page url="{page.url}" title="{page.title()[:80]}"/>\n'
                        f"<task>\n{instruction}\n</task>"
                    ),
                }
            ],
        }
    ]
    console.console.print(f"   [dim]│[/dim] [bold blue]reader[/] {instruction[:110]}")
    if trace:
        trace.write("reader_start", instruction=instruction)

    answer = ""
    for step in range(max_steps):
        response = llm.create(
            system=READER_SYSTEM,
            messages=messages,
            tools=tools,
            max_tokens=4_000,
            effort="low",
        )
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        answer = text_of(response.content) or answer
        if not tool_uses:
            break
        results = []
        for call in tool_uses:
            console.tool_call(call.name, dict(call.input), prefix="   [dim]│[/dim] ")
            outcome = box.dispatch(call.name, dict(call.input))
            console.tool_result(
                outcome.content if isinstance(outcome.content, str) else "<non-text>",
                outcome.is_error,
                prefix="   [dim]│[/dim] ",
                lines=2,
            )
            if outcome.terminal:
                answer = outcome.report or answer
                break
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": outcome.content,
                    "is_error": outcome.is_error,
                }
            )
        else:
            messages.append({"role": "user", "content": results})
            if step == max_steps - 2:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Step budget nearly spent. Answer now with what you have.",
                            }
                        ],
                    }
                )
            continue
        break

    if trace:
        trace.write("reader_done", answer=answer[:2000])
    console.console.print(f"   [dim]│[/dim] [blue]↩ returned {len(answer)} chars[/blue]")
    return answer or "(the reader returned nothing)"
