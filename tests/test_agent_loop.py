"""End-to-end agent loop tests against the demo site with a scripted model."""

from __future__ import annotations

from hydra.agent.console import AgentConsole
from hydra.agent.loop import AgentLoop
from hydra.config import AgentConfig
from tests.fake_llm import Block, FakeLLM


def _find_ref(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line and "[e" in line:
            return line[line.rindex("[e") + 1 : line.rindex("]")]
    raise AssertionError(f"no handle for {needle!r} in:\n{text}")


def _last_result(messages) -> str:
    for message in reversed(messages):
        if message["role"] == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block["content"]
                    return content if isinstance(content, str) else str(content)
    return ""


def build_agent(session, monkeypatch, script, safety="ask", confirm="y"):
    cfg = AgentConfig()
    cfg.safety.use_llm_judge = False
    cfg.safety.mode = safety
    cfg.max_steps = 12
    console = AgentConsole(quiet=True)
    monkeypatch.setattr(console, "confirm_action", lambda *a, **k: confirm)
    agent = AgentLoop(cfg, session, console)
    agent.llm = FakeLLM(script)
    agent.convo.llm = agent.llm
    agent.safety.llm = None
    return agent


def test_agent_completes_a_delete_task_through_the_gate(mail, monkeypatch):
    """Open a spam email, move it to trash through the site's confirm dialog,
    and report - all driven by tool calls, with the gate approving."""
    state = {}

    def step1(_messages):
        return [Block("tool_use", id="1", name="browser_snapshot", input={})]

    def step2(messages):
        state["ref"] = _find_ref(_last_result(messages), "Крипто-Доход")
        return [Block("tool_use", id="2", name="browser_click", input={"ref": state["ref"]})]

    def step3(messages):
        state["spam"] = _find_ref(_last_result(messages), "В корзину")
        return [Block("tool_use", id="3", name="browser_click", input={"ref": state["spam"]})]

    def step4(messages):
        state["modal"] = _last_result(messages)
        ref = _find_ref(state["modal"], "Переместить")
        return [Block("tool_use", id="4", name="browser_click", input={"ref": ref})]

    def step5(messages):
        state["after"] = _last_result(messages)
        return [
            Block(
                "tool_use",
                id="5",
                name="note",
                input={"action": "write", "key": "deleted", "value": "1 spam email"},
            ),
        ]

    def step6(_messages):
        return [
            Block(
                "tool_use",
                id="6",
                name="finish",
                input={"report": "Удалил 1 спам-письмо.", "status": "completed"},
            )
        ]

    agent = build_agent(mail, monkeypatch, [step1, step2, step3, step4, step5, step6])
    result = agent.run("Удали спам")

    assert result.status == "completed"
    assert "MODAL DIALOG IS OPEN" in state["modal"]
    assert result.notes["deleted"] == "1 spam email"
    assert "Крипто-Доход" not in mail.active_page().inner_text("body")


def test_running_out_of_steps_still_yields_a_report(mail, monkeypatch):
    def look(_m):
        return [Block("tool_use", id="s", name="browser_snapshot", input={})]

    def wrap_up(messages):
        assert "step budget is spent" in str(messages[-1]["content"])
        return [
            Block(
                "tool_use",
                id="f",
                name="finish",
                input={"report": "Нашёл 5 писем, спам не успел удалить."},
            )
        ]

    agent = build_agent(mail, monkeypatch, [look, look, wrap_up])
    agent.cfg.max_steps = 2
    result = agent.run("Удали спам")
    assert result.status == "partial"
    assert result.report == "Нашёл 5 писем, спам не успел удалить."


def test_a_refused_gate_stops_the_action_and_tells_the_agent_why(mail, monkeypatch):
    def step1(_m):
        return [Block("tool_use", id="1", name="browser_snapshot", input={})]

    def step2(messages):
        ref = _find_ref(_last_result(messages), "Крипто-Доход")
        return [Block("tool_use", id="2", name="browser_click", input={"ref": ref})]

    def step3(messages):
        ref = _find_ref(_last_result(messages), "В корзину")
        return [Block("tool_use", id="3", name="browser_click", input={"ref": ref})]

    def step4(messages):
        assert "BLOCKED by the safety layer" in _last_result(messages)
        return [
            Block(
                "tool_use",
                id="4",
                name="finish",
                input={"report": "Пользователь не подтвердил удаление.", "status": "partial"},
            )
        ]

    agent = build_agent(mail, monkeypatch, [step1, step2, step3, step4], confirm="n")
    result = agent.run("Удали спам")
    assert result.status == "partial"
    assert "Это спам" in mail.active_page().inner_text("body")


def test_repeated_identical_calls_trigger_a_replan_nudge(mail, monkeypatch):
    def loopy(_messages):
        return [Block("tool_use", id="x", name="browser_find", input={"query": "не существует"})]

    def done(_messages):
        return [
            Block("tool_use", id="y", name="finish", input={"report": "ok", "status": "partial"})
        ]

    agent = build_agent(mail, monkeypatch, [loopy, loopy, loopy, done])
    agent.run("что-то невозможное")
    text = str(agent.convo.messages)
    assert "Stop and" in text and "re-plan" in text


def test_stale_handles_produce_a_recoverable_error_not_a_crash(mail, monkeypatch):
    def step1(_m):
        return [Block("tool_use", id="1", name="browser_click", input={"ref": "e9999"})]

    def step2(messages):
        assert "fresh browser_snapshot" in _last_result(messages)
        return [
            Block("tool_use", id="2", name="finish", input={"report": "ok", "status": "partial"})
        ]

    agent = build_agent(mail, monkeypatch, [step1, step2])
    assert agent.run("клик по несуществующему").status == "partial"


def test_context_stays_flat_over_a_long_run(mail, monkeypatch):
    """Twelve snapshots of the same page must not grow the transcript twelvefold."""
    sizes = []

    def snap(_m):
        sizes.append(len(agent.convo._serialised()))
        return [Block("tool_use", id="s", name="browser_snapshot", input={})]

    def done(_m):
        return [
            Block("tool_use", id="d", name="finish", input={"report": "ok", "status": "completed"})
        ]

    agent = build_agent(mail, monkeypatch, [snap] * 10 + [done])
    agent.run("смотри на страницу много раз")
    growth = sizes[-1] - sizes[3]
    assert agent.convo.pruned_count >= 5
    assert growth < sizes[3], f"transcript grew by {growth} chars over 7 extra observations"


def test_a_reading_subagent_cannot_act(mail):
    """Reader role: schemas exclude click; dispatch also refuses mutating tools.
    finish MAY be available in hydra's reader surface — do not assert it is blocked."""
    from hydra.agent.safety import SafetyPolicy
    from hydra.agent.tools import Toolbox
    from hydra.config import AgentConfig, SafetyConfig

    cfg = AgentConfig()
    box = Toolbox(
        mail,
        cfg,
        SafetyPolicy(cfg=SafetyConfig(use_llm_judge=False)),
        readonly=True,
        role="reader",
    )
    names = {t["name"] for t in box.schemas(readonly=True)}
    assert "browser_click" not in names

    outcome = box.dispatch("browser_click", {"ref": "e1"})
    assert outcome.is_error and "not available" in outcome.content


def test_a_rate_limit_is_waited_out_not_fatal(mail, monkeypatch):
    """Losing a run that was going fine because the provider rate-limited one
    call is the wrong trade: waiting is almost always cheaper than starting over."""
    import anthropic
    import httpx2 as httpx
    import hydra.agent.loop as loop_mod

    calls = {"n": 0}

    def httpx_response(status):
        return httpx.Response(status, request=httpx.Request("POST", "https://x.test"))

    def flaky(_m):
        calls["n"] += 1
        if calls["n"] == 1:
            raise anthropic.RateLimitError(
                "rate limited",
                response=httpx_response(429),
                body=None,
            )
        return [Block("tool_use", name="finish", input={"report": "ok", "status": "completed"})]

    waited: list[float] = []
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: waited.append(s))

    agent = build_agent(mail, monkeypatch, [flaky, flaky])
    result = agent.run("что-нибудь")

    assert result.status == "completed"
    assert waited, "the loop must back off rather than give up"


def test_page_content_arrives_fenced_as_untrusted(mail):
    """Everything the site wrote is fenced so transcript never blurs site/user."""
    from hydra.agent.safety import SafetyPolicy
    from hydra.agent.tools import UNTRUSTED_OPEN, Toolbox
    from hydra.config import AgentConfig, SafetyConfig

    box = Toolbox(mail, AgentConfig(), SafetyPolicy(cfg=SafetyConfig(use_llm_judge=False)))
    for call, args in [
        ("browser_snapshot", {}),
        ("browser_find", {"query": "Входящие"}),
        ("browser_read_text", {"max_chars": 200}),
    ]:
        outcome = box.dispatch(call, args)
        assert not outcome.is_error, call
        assert outcome.content.startswith(UNTRUSTED_OPEN), call


def test_parallel_delegate_is_invoked_and_merges_reports(mail, monkeypatch):
    """FakeLLM asks for parallel_delegate with 2 tasks; patched fan-out returns a merge."""
    seen: list[list] = []

    def step1(_m):
        return [
            Block(
                "tool_use",
                id="1",
                name="parallel_delegate",
                input={
                    "tasks": [
                        {"goal": "read inbox count"},
                        {"goal": "find spam folder"},
                    ]
                },
            )
        ]

    def step2(messages):
        assert "WORKER-A" in _last_result(messages)
        return [
            Block(
                "tool_use",
                id="2",
                name="finish",
                input={"report": "merged ok", "status": "completed"},
            )
        ]

    agent = build_agent(mail, monkeypatch, [step1, step2])

    def fake_parallel(tasks):
        seen.append(tasks)
        return "WORKER-A: done\nWORKER-B: done"

    monkeypatch.setattr(agent, "_parallel_delegate", fake_parallel)
    monkeypatch.setattr(agent.tools, "parallel_delegate_fn", fake_parallel)

    result = agent.run("do two things in parallel")
    assert result.status == "completed"
    assert len(seen) == 1 and len(seen[0]) == 2
