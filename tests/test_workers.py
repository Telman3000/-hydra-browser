"""Workers must be action-capable; readers must not; main owns parallel_delegate."""

from __future__ import annotations

from hydra.agent.safety import SafetyPolicy
from hydra.agent.tools import Toolbox
from hydra.agent.workers import WorkerResult, merge_worker_reports
from hydra.config import AgentConfig, SafetyConfig


def _box(role: str, readonly: bool = False) -> Toolbox:
    return Toolbox(
        session=None,  # type: ignore[arg-type]
        cfg=AgentConfig(),
        safety=SafetyPolicy(cfg=SafetyConfig(use_llm_judge=False)),
        readonly=readonly,
        role=role,
    )


def test_merge_worker_reports_orders_and_includes_errors():
    results = [
        WorkerResult(2, "completed", "done B", steps=3),
        WorkerResult(1, "failed", "", steps=1, error="boom"),
    ]
    results.sort(key=lambda r: r.worker_id)
    text = merge_worker_reports(results)
    assert "### worker 1" in text
    assert "### worker 2" in text
    assert "error: boom" in text
    assert "done B" in text
    assert text.index("worker 1") < text.index("worker 2")


def test_worker_schemas_include_action_tools():
    names = {t["name"] for t in _box("worker").schemas(role="worker")}
    for needed in ("browser_click", "browser_type", "browser_navigate", "browser_select"):
        assert needed in names, f"worker must be able to {needed}"
    assert "parallel_delegate" not in names
    assert "delegate_reading" not in names
    assert "ask_user" not in names
    assert "finish" in names


def test_reader_schemas_exclude_mutations():
    names = {t["name"] for t in _box("reader", readonly=True).schemas(readonly=True, role="reader")}
    for forbidden in ("browser_click", "browser_type", "browser_navigate", "browser_press"):
        assert forbidden not in names
    for allowed in ("browser_snapshot", "browser_find", "browser_read_text", "browser_scroll"):
        assert allowed in names
    assert "parallel_delegate" not in names


def test_main_schemas_include_parallel_delegate():
    names = {t["name"] for t in _box("main").schemas(role="main")}
    assert "parallel_delegate" in names
    assert "delegate_reading" in names
    assert "browser_click" in names
    assert "browser_back" in names


def test_empty_merge_is_explicit():
    assert merge_worker_reports([]) == "(no workers ran)"
