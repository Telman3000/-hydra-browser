"""Tool schemas and dispatch — including parallel_delegate for action workers."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError

from ..browser import actions as A
from ..browser import snapshot as S
from ..browser.session import BrowserSession
from ..config import AgentConfig
from .safety import SafetyPolicy

UNTRUSTED_OPEN = S.UNTRUSTED_OPEN
UNTRUSTED_CLOSE = S.UNTRUSTED_CLOSE


@dataclass
class ToolOutcome:
    content: Any
    is_error: bool = False
    observation: str | None = None
    terminal: bool = False
    report: str | None = None
    status: str | None = None


def untrusted(text: str) -> str:
    return S.fence_untrusted(text)


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


REF = {"type": "string", "description": "Element handle from a snapshot or find result, e.g. 'e42'."}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "browser_snapshot",
        "Look at the current page: semantic outline with [eNN] handles on actionable controls.",
        {
            "scope": {
                "type": "string",
                "enum": ["viewport", "page"],
                "description": "'viewport' (default) or full 'page'.",
            },
            "interactive_only": {
                "type": "boolean",
                "description": "Only list controls, dropping surrounding text.",
            },
        },
        [],
    ),
    _schema(
        "browser_find",
        "Search the rendered page for visible text; returns matching elements with handles.",
        {
            "query": {"type": "string", "description": "Visible text to look for, case-insensitive."},
            "limit": {"type": "integer", "description": "Max matches (default 15)."},
        },
        ["query"],
    ),
    _schema(
        "browser_read_text",
        "Read readable text of the page or one element, in chunks.",
        {
            "ref": {**REF, "description": "Optional: read only inside this element."},
            "start": {"type": "integer", "description": "Character offset (default 0)."},
            "max_chars": {"type": "integer", "description": "How much to read (default 4000)."},
        },
        [],
    ),
    _schema(
        "browser_click",
        "Click an element by handle. Returns what changed on the page.",
        {
            "ref": REF,
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "modifiers": {
                "type": "array",
                "items": {"type": "string", "enum": ["Alt", "Control", "Meta", "Shift"]},
            },
            "why": {
                "type": "string",
                "description": "Short phrase: what you expect this click to achieve.",
            },
        },
        ["ref"],
    ),
    _schema(
        "browser_type",
        "Type text into a field by handle. Never use for passwords or card details.",
        {
            "ref": REF,
            "text": {"type": "string"},
            "submit": {"type": "boolean", "description": "Press Enter after typing."},
            "clear": {"type": "boolean", "description": "Clear the field first (default true)."},
        },
        ["ref", "text"],
    ),
    _schema(
        "browser_select",
        "Choose options in a <select> by visible labels.",
        {"ref": REF, "values": {"type": "array", "items": {"type": "string"}}},
        ["ref", "values"],
    ),
    _schema(
        "browser_check",
        "Set a checkbox or radio to checked/unchecked.",
        {"ref": REF, "checked": {"type": "boolean"}},
        ["ref", "checked"],
    ),
    _schema(
        "browser_scroll",
        "Scroll the page or a scrollable container by handle.",
        {
            "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
            "amount": {"type": "integer", "description": "Screens to scroll (default 1)."},
            "ref": {**REF, "description": "Optional: scroll inside this container."},
        },
        ["direction"],
    ),
    _schema(
        "browser_press",
        "Press a key: Enter, Escape, Tab, ArrowDown, PageDown, Control+a, etc.",
        {"key": {"type": "string"}},
        ["key"],
    ),
    _schema(
        "browser_wait",
        "Wait for text, text_gone, idle, or seconds.",
        {
            "condition": {"type": "string", "enum": ["text", "text_gone", "idle", "seconds"]},
            "value": {"type": "string"},
            "timeout_ms": {"type": "integer", "description": "Default 8000, max 30000."},
        },
        ["condition"],
    ),
    _schema(
        "browser_tabs",
        "List, select, open, or close tabs.",
        {
            "action": {"type": "string", "enum": ["list", "select", "new", "close"]},
            "index": {"type": "integer"},
            "url": {"type": "string", "description": "Optional url for 'new'."},
        },
        ["action"],
    ),
    _schema(
        "browser_navigate",
        "Go to a url, or move through history (back/forward/reload).",
        {
            "action": {"type": "string", "enum": ["goto", "back", "forward", "reload"]},
            "url": {"type": "string", "description": "Required for 'goto'."},
        },
        ["action"],
    ),
    _schema(
        "browser_back",
        "Go back in the current tab's history.",
        {},
        [],
    ),
    _schema(
        "browser_screenshot",
        "Take a screenshot of the viewport. Use when the text outline is not enough "
        "(chart, canvas, confusing layout). Prefer the snapshot when text is enough.",
        {},
        [],
    ),
    _schema(
        "note",
        "Scratchpad: write stores a fact; read returns all notes. Survives compaction.",
        {
            "action": {"type": "string", "enum": ["write", "read"]},
            "key": {"type": "string"},
            "value": {"type": "string"},
        },
        ["action"],
    ),
    _schema(
        "ask_user",
        "Ask the user and wait. For missing info or steps only they can do in the browser.",
        {"question": {"type": "string"}},
        ["question"],
    ),
    _schema(
        "finish",
        "End the task and report. Call when done or when it cannot be finished.",
        {
            "report": {"type": "string"},
            "status": {"type": "string", "enum": ["completed", "partial", "failed"]},
        },
        ["report", "status"],
    ),
    _schema(
        "parallel_delegate",
        "Spawn up to N action-capable workers in parallel — each with its own Chromium "
        "window. Workers can click, type and navigate. Cookies are inherited from the "
        "main session. Use when independent sub-goals can run concurrently. Returns "
        "merged worker reports.",
        {
            "tasks": {
                "type": "array",
                "description": "List of worker assignments.",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "Self-contained goal for this worker.",
                        },
                        "start_url": {
                            "type": "string",
                            "description": "Optional URL to open first in the worker window.",
                        },
                    },
                    "required": ["goal"],
                },
            },
        },
        ["tasks"],
    ),
    _schema(
        "delegate_reading",
        "Hand a reading job to a read-only helper in the SHARED main browser. "
        "It can look/find/read/scroll but cannot click, type or navigate.",
        {
            "instruction": {
                "type": "string",
                "description": "Self-contained brief: what to read and how to return it.",
            },
            "max_steps": {"type": "integer", "description": "Step budget (default 14)."},
        },
        ["instruction"],
    ),
]

READONLY_TOOL_NAMES = {
    "browser_snapshot",
    "browser_find",
    "browser_read_text",
    "browser_scroll",
    "browser_screenshot",
    "browser_tabs",
    "browser_wait",
    "note",
    "finish",
}

# Workers act but do not spawn more workers or ask the human mid-flight.
WORKER_EXCLUDED = {"parallel_delegate", "delegate_reading", "ask_user"}

ORCHESTRATOR_ONLY = {"parallel_delegate", "delegate_reading"}


class Toolbox:
    """Executes tool calls against the browser and formats results for the model."""

    def __init__(
        self,
        session: BrowserSession,
        cfg: AgentConfig,
        safety: SafetyPolicy,
        *,
        ask_user: Callable[[str], str] | None = None,
        delegate_reading: Callable[[str, int], str] | None = None,
        parallel_delegate: Callable[[list[dict[str, Any]]], str] | None = None,
        notes: dict[str, str] | None = None,
        readonly: bool = False,
        role: str = "main",  # main | worker | reader
    ) -> None:
        self.session = session
        self.cfg = cfg
        self.safety = safety
        self.ask_user = ask_user
        self.delegate_reading_fn = delegate_reading
        self.parallel_delegate_fn = parallel_delegate
        self.notes = notes if notes is not None else {}
        self.snapshot: S.Snapshot | None = None
        self.last_verdict = None
        self.readonly = readonly
        self.role = role
        self.allowed = {t["name"] for t in self.schemas(readonly=readonly, role=role)}

    def schemas(self, readonly: bool = False, role: str | None = None) -> list[dict[str, Any]]:
        role = role or self.role
        if readonly or role == "reader":
            return [t for t in TOOL_SCHEMAS if t["name"] in READONLY_TOOL_NAMES]
        if role == "worker":
            return [t for t in TOOL_SCHEMAS if t["name"] not in WORKER_EXCLUDED]
        return list(TOOL_SCHEMAS)

    def refresh(self, scope: str = "viewport", interactive_only: bool = False) -> S.Snapshot:
        self.snapshot = S.capture(self.session, scope=scope, interactive_only=interactive_only)
        return self.snapshot

    def _ensure_snapshot(self) -> S.Snapshot:
        if self.snapshot is None:
            self.refresh()
        assert self.snapshot is not None
        return self.snapshot

    def label(self, ref: str | None) -> str:
        if not ref or self.snapshot is None:
            return ""
        for node in self.snapshot.nodes:
            if node.get("ref") == ref:
                return f'{node["role"]} "{node.get("name", "")}"'
        return ref

    def _after_action(self, message: str) -> str:
        before = self.snapshot
        after = self.refresh()
        parts = [message]
        for event in self.session.drain_dialogs():
            parts.append(
                f'a native {event.kind} dialog appeared saying "{event.message}" '
                f"and was {event.handled_as}ed"
            )
        parts.append(untrusted(S.diff(before, after, max_lines=40)))
        return "\n\n".join(parts)

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        if name not in self.allowed:
            return ToolOutcome(
                f"{name!r} is not available to you. Available tools: "
                f"{', '.join(sorted(self.allowed))}.",
                is_error=True,
            )
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return ToolOutcome(f"unknown tool {name!r}", is_error=True)
        try:
            return handler(args)
        except (A.ActionError, S.StaleRefError) as exc:
            return ToolOutcome(str(exc), is_error=True)
        except PlaywrightError as exc:
            return ToolOutcome(
                f"browser error: {str(exc).splitlines()[0]}. Take a fresh browser_snapshot "
                f"and reassess - the page may have changed under you.",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(f"{type(exc).__name__}: {exc}", is_error=True)

    def _t_browser_snapshot(self, args: dict[str, Any]) -> ToolOutcome:
        scope = args.get("scope", "viewport")
        snap = self.refresh(scope=scope, interactive_only=bool(args.get("interactive_only")))
        return ToolOutcome(
            untrusted(snap.render(self.cfg.context.snapshot_chars)),
            observation=f"snapshot of {snap.url[:70]}",
        )

    def _t_browser_find(self, args: dict[str, Any]) -> ToolOutcome:
        query = args["query"]
        matches = A.find(self.session, query, int(args.get("limit", 15)))
        if not matches:
            return ToolOutcome(
                f"nothing on the page contains {query!r}. It may be further down "
                f"(browser_scroll), behind a control you have not opened, or worded "
                f"differently - try a shorter fragment, or take a browser_snapshot."
            )
        lines = [f"{len(matches)} match(es) for {query!r}:"]
        for m in matches:
            lines.append(f'- {m["role"]} "{m["name"]}" [{m["ref"]}]  — context: {m["context"]}')
        self._ensure_snapshot()
        return ToolOutcome(untrusted("\n".join(lines)), observation=f"find {query!r}")

    def _t_browser_read_text(self, args: dict[str, Any]) -> ToolOutcome:
        max_chars = min(int(args.get("max_chars", self.cfg.context.read_chars)), 12_000)
        res = A.read_text(self.session, args.get("ref"), int(args.get("start", 0)), max_chars)
        more = res["start"] + res["returned"]
        tail = ""
        if more < res["total"]:
            tail = (
                f"\n\n[{more}/{res['total']} characters read. Continue with "
                f"browser_read_text(start={more}) if you need the rest.]"
            )
        return ToolOutcome(untrusted(res["text"]) + tail, observation="page text")

    def _t_browser_click(self, args: dict[str, Any]) -> ToolOutcome:
        snap = self._ensure_snapshot()
        ref = args["ref"]
        allowed, verdict = self._gate("browser_click", args, self.label(ref))
        if not allowed:
            return self._refused(verdict)
        msg = A.click(self.session, snap, ref, args.get("button", "left"), args.get("modifiers"))
        return ToolOutcome(self._after_action(msg), observation="page after click")

    def _t_browser_type(self, args: dict[str, Any]) -> ToolOutcome:
        snap = self._ensure_snapshot()
        allowed, verdict = self._gate("browser_type", args, self.label(args["ref"]))
        if not allowed:
            return self._refused(verdict)
        msg = A.type_text(
            self.session,
            snap,
            args["ref"],
            args["text"],
            clear=args.get("clear", True),
            submit=bool(args.get("submit")),
        )
        return ToolOutcome(self._after_action(msg), observation="page after typing")

    def _t_browser_select(self, args: dict[str, Any]) -> ToolOutcome:
        snap = self._ensure_snapshot()
        allowed, verdict = self._gate("browser_select", args, self.label(args["ref"]))
        if not allowed:
            return self._refused(verdict)
        msg = A.select_option(self.session, snap, args["ref"], list(args["values"]))
        return ToolOutcome(self._after_action(msg), observation="page after select")

    def _t_browser_check(self, args: dict[str, Any]) -> ToolOutcome:
        snap = self._ensure_snapshot()
        allowed, verdict = self._gate("browser_check", args, self.label(args["ref"]))
        if not allowed:
            return self._refused(verdict)
        msg = A.set_checked(self.session, snap, args["ref"], bool(args["checked"]))
        return ToolOutcome(self._after_action(msg), observation="page after toggle")

    def _t_browser_scroll(self, args: dict[str, Any]) -> ToolOutcome:
        snap = self._ensure_snapshot()
        msg = A.scroll(
            self.session,
            snap,
            args.get("direction", "down"),
            int(args.get("amount", 1)),
            args.get("ref"),
        )
        return ToolOutcome(self._after_action(msg), observation="page after scroll")

    def _t_browser_press(self, args: dict[str, Any]) -> ToolOutcome:
        allowed, verdict = self._gate("browser_press", args, f"key {args['key']}")
        if not allowed:
            return self._refused(verdict)
        msg = A.press_key(self.session, args["key"])
        return ToolOutcome(self._after_action(msg), observation="page after key press")

    def _t_browser_wait(self, args: dict[str, Any]) -> ToolOutcome:
        timeout = min(int(args.get("timeout_ms", 8_000)), 30_000)
        msg = A.wait_for(self.session, args["condition"], args.get("value"), timeout)
        return ToolOutcome(self._after_action(msg), observation="page after wait")

    def _t_browser_tabs(self, args: dict[str, Any]) -> ToolOutcome:
        action = args["action"]
        if action == "list":
            return ToolOutcome(json.dumps(self.session.tab_list(), ensure_ascii=False, indent=1))
        if action == "select":
            self.session.select_tab(int(args["index"]))
        elif action == "new":
            self.session.new_tab(args.get("url"))
        elif action == "close":
            self.session.close_tab(int(args["index"]))
        else:
            return ToolOutcome(f"unknown tabs action {action!r}", is_error=True)
        self.snapshot = None
        snap = self.refresh()
        return ToolOutcome(
            f"tabs: {json.dumps(self.session.tab_list(), ensure_ascii=False)}\n\n"
            f"{snap.render(self.cfg.context.snapshot_chars)}",
            observation="page after tab switch",
        )

    def _t_browser_navigate(self, args: dict[str, Any]) -> ToolOutcome:
        allowed, verdict = self._gate("browser_navigate", args, args.get("url", args["action"]))
        if not allowed:
            return self._refused(verdict)
        msg = A.navigate(self.session, args["action"], args.get("url"))
        self.snapshot = None
        snap = self.refresh()
        return ToolOutcome(
            f"{msg}\n\n{untrusted(snap.render(self.cfg.context.snapshot_chars))}",
            observation=f"page {snap.url[:70]}",
        )

    def _t_browser_back(self, _args: dict[str, Any]) -> ToolOutcome:
        allowed, verdict = self._gate("browser_back", {}, "back")
        if not allowed:
            return self._refused(verdict)
        msg = A.navigate(self.session, "back")
        self.snapshot = None
        snap = self.refresh()
        return ToolOutcome(
            f"{msg}\n\n{untrusted(snap.render(self.cfg.context.snapshot_chars))}",
            observation=f"page {snap.url[:70]}",
        )

    def _t_browser_screenshot(self, _args: dict[str, Any]) -> ToolOutcome:
        page = self.session.active_page()
        data = page.screenshot(type="jpeg", quality=62, scale="css")
        return ToolOutcome(
            [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(data).decode(),
                    },
                },
                {"type": "text", "text": f"screenshot of {page.url[:120]}"},
            ],
            observation="screenshot",
        )

    def _t_note(self, args: dict[str, Any]) -> ToolOutcome:
        if args["action"] == "write":
            key, value = args.get("key"), args.get("value")
            if not key or value is None:
                return ToolOutcome("note(write) needs both key and value", is_error=True)
            self.notes[key] = value
            return ToolOutcome(f"noted {key!r} ({len(self.notes)} notes stored)")
        if not self.notes:
            return ToolOutcome("no notes stored yet")
        return ToolOutcome("\n".join(f"- {k}: {v}" for k, v in self.notes.items()))

    def _t_ask_user(self, args: dict[str, Any]) -> ToolOutcome:
        if self.ask_user is None:
            return ToolOutcome("no user is available to answer in this run", is_error=True)
        answer = self.ask_user(args["question"])
        return ToolOutcome(f"the user answered: {answer}")

    def _t_finish(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            "task closed",
            terminal=True,
            report=args.get("report", ""),
            status=args.get("status", "completed"),
        )

    def _t_parallel_delegate(self, args: dict[str, Any]) -> ToolOutcome:
        if self.parallel_delegate_fn is None:
            return ToolOutcome("parallel_delegate is not available in this run", is_error=True)
        tasks = args.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            return ToolOutcome("parallel_delegate requires a non-empty tasks list", is_error=True)
        max_n = self.cfg.workers.max_workers
        if len(tasks) > max_n:
            return ToolOutcome(
                f"too many tasks ({len(tasks)}); max_workers={max_n}. Split and call again.",
                is_error=True,
            )
        merged = self.parallel_delegate_fn(tasks)
        return ToolOutcome(f"<parallel_worker_results>\n{merged}\n</parallel_worker_results>")

    def _t_delegate_reading(self, args: dict[str, Any]) -> ToolOutcome:
        if self.delegate_reading_fn is None:
            return ToolOutcome("delegate_reading is not available in this run", is_error=True)
        answer = self.delegate_reading_fn(
            args["instruction"],
            int(args.get("max_steps", self.cfg.subagent_max_steps)),
        )
        return ToolOutcome(f"<reader_result>\n{answer}\n</reader_result>")

    def _gate(self, tool: str, args: dict[str, Any], label: str) -> tuple[bool, Any]:
        url = self.session.active_page().url if self.session.page else ""
        allowed, verdict = self.safety.gate(tool, args, label, url)
        self.last_verdict = verdict
        return allowed, verdict

    @staticmethod
    def _refused(verdict: Any) -> ToolOutcome:
        return ToolOutcome(
            f"BLOCKED by the safety layer ({verdict.risk}): {verdict.reason}. "
            f"Do not attempt it again or look for a workaround - continue with the rest "
            f"of the task and mention this in your report.",
            is_error=True,
        )
