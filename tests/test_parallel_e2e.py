"""Real parallel workers with FakeLLM — no API key, two Chromium processes."""

from __future__ import annotations

from pathlib import Path

import hydra.agent.workers as W
from hydra.agent.console import AgentConsole
from hydra.agent.workers import merge_worker_reports, run_parallel_workers
from hydra.config import AgentConfig
from tests.fake_llm import Block, FakeLLM


def test_parallel_workers_act_in_isolated_browsers(demo_url, monkeypatch, tmp_path):
    def make_fake(*_a, **_k):
        return FakeLLM(
            [
                lambda _m: [Block("tool_use", name="browser_snapshot", input={})],
                lambda _m: [
                    Block(
                        "tool_use",
                        name="finish",
                        input={"report": "saw page", "status": "completed"},
                    )
                ],
            ]
        )

    monkeypatch.setattr(W, "LLM", make_fake)

    cfg = AgentConfig()
    cfg.browser.headless = True
    cfg.workers.max_workers = 2
    cfg.workers.max_steps = 5
    cfg.safety.use_llm_judge = False

    url = f"{demo_url}/mail.html"
    results = run_parallel_workers(
        tasks=[
            {"goal": "Snapshot the page and finish", "start_url": url},
            {"goal": "Snapshot the page and finish", "start_url": url},
        ],
        storage_state_path=None,
        cfg=cfg,
        console=AgentConsole(quiet=True),
        run_dir=tmp_path / "workers",
    )
    text = merge_worker_reports(results)
    assert len(results) == 2
    assert all(r.status == "completed" for r in results), text
    assert all(r.steps >= 2 for r in results), text
    assert "worker 1" in text and "worker 2" in text
