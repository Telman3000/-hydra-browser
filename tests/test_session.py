"""Persistent profile: cookies / localStorage must survive a process restart."""

from __future__ import annotations

from hydra.browser import snapshot as S
from hydra.browser.session import BrowserSession


def _start(tmp_path, url):
    session = BrowserSession(user_data_dir=tmp_path / "profile", headless=True)
    session.start(url)
    session.settle(quiet_ms=300)
    return session


def test_a_login_survives_a_restart_of_the_agent(tmp_path, demo_url):
    session = _start(tmp_path, f"{demo_url}/mail.html")
    session.active_page().evaluate(
        "() => { document.cookie = 'session_id=abc123; path=/; max-age=86400';"
        "  localStorage.setItem('signed_in_as', 'hydra@example.test'); }"
    )
    session.close()

    again = _start(tmp_path, f"{demo_url}/mail.html")
    try:
        cookie = again.active_page().evaluate("() => document.cookie")
        stored = again.active_page().evaluate("() => localStorage.getItem('signed_in_as')")
        assert "session_id=abc123" in cookie
        assert stored == "hydra@example.test"
    finally:
        again.close()


def test_a_fresh_profile_starts_signed_out(tmp_path, demo_url):
    session = _start(tmp_path / "a", f"{demo_url}/mail.html")
    session.active_page().evaluate("() => localStorage.setItem('signed_in_as', 'hydra')")
    session.close()

    other = _start(tmp_path / "b", f"{demo_url}/mail.html")
    try:
        assert other.active_page().evaluate("() => localStorage.getItem('signed_in_as')") is None
    finally:
        other.close()


def test_the_browser_is_visible_by_default(tmp_path):
    assert BrowserSession(user_data_dir=tmp_path).headless is False


def test_a_tab_the_site_opens_becomes_the_active_one(session, demo_url):
    session.goto(f"{demo_url}/mail.html")
    before = len(session.pages)

    opened = session.context.new_page()
    opened.goto(f"{demo_url}/shop.html", wait_until="domcontentloaded")
    session.settle(quiet_ms=500)

    assert len(session.pages) == before + 1
    assert session.page is opened
    assert "Вкусно и Быстро" in S.capture(session).render()

    tabs = session.tab_list()
    assert sum(1 for t in tabs if t["active"]) == 1


def test_a_site_that_redirects_on_arrival_is_not_a_failure(session, demo_url):
    session.goto(f"{demo_url}/redirect.html")
    session.settle(quiet_ms=400)
    assert session.active_page().url.endswith("/index.html")
    assert "Демо-стенд" in session.active_page().title()


def test_export_storage_state_writes_cookies(tmp_path, demo_url):
    session = _start(tmp_path, f"{demo_url}/mail.html")
    try:
        session.active_page().evaluate(
            "() => { document.cookie = 'hydra_tok=xyz; path=/; max-age=86400'; }"
        )
        out = tmp_path / "state.json"
        session.export_storage_state(out)
        assert out.exists() and out.stat().st_size > 20
        raw = out.read_text(encoding="utf-8")
        assert "hydra_tok" in raw or "cookies" in raw
    finally:
        session.close()
