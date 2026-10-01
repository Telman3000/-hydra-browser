"""Runtime knobs — nothing hard-coded deeper than this module."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Playwright resolves its ffmpeg (needed for --record-video) under this path; a
# project-local copy avoids depending on its CDN, which is often unreachable.
_LOCAL_PW = PROJECT_ROOT / ".pw-browsers"
_SET_PW = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
if _LOCAL_PW.is_dir() and not (_SET_PW and any(Path(_SET_PW).glob("ffmpeg-*"))):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_LOCAL_PW)

_PROVIDER = os.getenv("AGENT_PROVIDER", "anthropic").lower()
_DEFAULT_MAIN = (
    os.getenv("AGENT_MODEL")
    or ("gpt-4.1-mini" if _PROVIDER == "openai" else "claude-sonnet-4-5")
)
_DEFAULT_SMALL = (
    os.getenv("AGENT_SMALL_MODEL")
    or ("gpt-4o-mini" if _PROVIDER == "openai" else "claude-haiku-4-5")
)


@dataclass
class ModelConfig:
    provider: str = _PROVIDER  # anthropic | openai
    main: str = _DEFAULT_MAIN
    small: str = _DEFAULT_SMALL
    effort: str = os.getenv("AGENT_EFFORT", "high")
    max_tokens: int = 4_000
    profile: str = os.getenv("AGENT_API_PROFILE", "anthropic")
    base_url: str | None = os.getenv("ANTHROPIC_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None


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
    layout: str = os.getenv("AGENT_WINDOW_LAYOUT", "default")  # default | split


@dataclass
class WorkerConfig:
    """Parallel action-capable workers — each owns an isolated Chromium window."""

    max_workers: int = int(os.getenv("AGENT_MAX_WORKERS", "3"))
    max_steps: int = int(os.getenv("AGENT_WORKER_MAX_STEPS", "20"))
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
