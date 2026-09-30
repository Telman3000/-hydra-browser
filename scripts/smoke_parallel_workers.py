"""Smoke: real parallel workers with FakeLLM (no API key)."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from hydra.agent.console import AgentConsole
from hydra.agent.workers import merge_worker_reports, run_parallel_workers
from hydra.config import AgentConfig
from tests.fake_llm import Block, FakeLLM
import hydra.agent.workers as W


def make_fake(*_args, **_kwargs):
    def step_snap(_m):
        return [Block("tool_use", name="browser_snapshot", input={})]

    def step_finish(_m):
        return [
            Block(
                "tool_use",
                name="finish",
                input={"report": "saw page", "status": "completed"},
            )
        ]

    return FakeLLM([step_snap, step_finish])


W.LLM = make_fake  # type: ignore[misc, assignment]

cfg = AgentConfig()
cfg.browser.headless = True
cfg.workers.max_workers = 2
cfg.workers.max_steps = 5
cfg.safety.use_llm_judge = False
console = AgentConsole(quiet=True)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]

site = ROOT / "demo" / "site"
assert site.is_dir(), site
proc = subprocess.Popen(
    [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "-d", str(site)],
    cwd=str(ROOT),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
)
for _ in range(50):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            break
    except OSError:
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
            raise RuntimeError(f"http.server died: {err}")
        time.sleep(0.1)
else:
    raise RuntimeError("http.server did not start")

url = f"http://127.0.0.1:{port}/mail.html"
try:
    results = run_parallel_workers(
        tasks=[
            {"goal": "Open the page and report what you see briefly", "start_url": url},
            {"goal": "Open the page and report inbox presence", "start_url": url},
        ],
        storage_state_path=None,
        cfg=cfg,
        console=console,
        run_dir=ROOT / "runs" / "smoke_workers",
    )
    print(merge_worker_reports(results))
    assert len(results) == 2, results
    assert all(r.status == "completed" for r in results), results
    assert all(r.steps >= 1 for r in results), results
    print("SMOKE_OK", [(r.worker_id, r.status, r.steps) for r in results])
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
