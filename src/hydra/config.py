"""Runtime knobs — nothing hard-coded deeper than this module."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ModelConfig:
    main: str = os.getenv("AGENT_MODEL", "claude-sonnet-4-5")
    small: str = os.getenv("AGENT_SMALL_MODEL", "claude-haiku-4-5")
    effort: str = os.getenv("AGENT_EFFORT", "high")
    max_tokens: int = 4_000
    profile: str = os.getenv("AGENT_API_PROFILE", "anthropic")
    base_url: str | None = os.getenv("ANTHROPIC_BASE_URL") or None


@dataclass
class ContextConfig:
    budget_tokens: int = int(os.getenv("AGENT_CONTEXT_BUDGET", "70000"))
    compact_at: float = 0.75
    keep_observations: int = 3
    keep_recent_turns: int = 6
    snapshot_chars: int = 6_000
    read_chars: int = 4_000
    tool_result_chars: int = 8_000
    verify_tokens_every: int = 6


@dataclass
class SafetyConfig:
    mode: str = os.getenv("AGENT_SAFETY", "ask")  # ask | strict | yolo
    use_llm_judge: bool = os.getenv("AGENT_LLM_JUDGE", "1") != "0"


@dataclass
class BrowserConfig:
    profile_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("AGENT_PROFILE_DIR", str(PROJECT_ROOT / "profiles" / "default"))
        )
    )
    headless: bool = os.getenv("AGENT_HEADLESS", "0") == "1"
    start_url: str = os.getenv("AGENT_START_URL", "about:blank")
    slow_mo_ms: int = int(os.getenv("AGENT_SLOW_MO", "0"))
    record_video: bool = False


@dataclass
class WorkerConfig:
    """Parallel action-capable workers — each owns an isolated Chromium window."""

    max_workers: int = int(os.getenv("AGENT_MAX_WORKERS", "3"))
    max_steps: int = int(os.getenv("AGENT_WORKER_MAX_STEPS", "20"))
    # Workers inherit cookies from the orchestrator via storage_state export.
    inherit_session: bool = True


@dataclass
class AgentConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    workers: WorkerConfig = field(default_factory=WorkerConfig)
    max_steps: int = int(os.getenv("AGENT_MAX_STEPS", "60"))
    subagent_max_steps: int = 14
    runs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "runs")
