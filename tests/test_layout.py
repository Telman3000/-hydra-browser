"""Window placement for screen recordings."""

from hydra.browser import layout as L


def test_split_leaves_the_left_of_the_screen_free(monkeypatch):
    monkeypatch.setattr(L, "screen_size", lambda: (2000, 1000))
    x, y, w, h = L.main_window_rect("split")
    assert x == 800 and x + w == 2000 and h == 1000


def test_split_workers_tile_inside_the_browser_area(monkeypatch):
    monkeypatch.setattr(L, "screen_size", lambda: (2000, 1000))
    rects = [L.worker_window_rect("split", i, 3) for i in range(3)]
    for x, y, w, h in rects:
        assert x >= 800 and x + w <= 2000 and y + h <= 1000
    assert rects[0][1] == rects[1][1] and rects[0][0] != rects[1][0]
    assert rects[2][2] == 1200  # the odd one out spans the full width


def test_tables_become_lists_in_a_narrow_terminal():
    from hydra.agent.console import tables_as_lists

    md = "Итог:\n\n| Вакансия | Компания | Опыт |\n|---|---|---|\n| AI-инженер | Сбер | 3-6 лет |\n\nконец"
    out = tables_as_lists(md)
    assert "- **AI-инженер** — Компания: Сбер; Опыт: 3-6 лет" in out
    assert "|" not in out and out.startswith("Итог:") and out.rstrip().endswith("конец")


def test_default_layout_keeps_maximized_main_window():
    assert L.window_args(L.main_window_rect("default")) == ["--start-maximized"]
