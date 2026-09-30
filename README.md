# Hydra

**Параллельный браузерный агент:** оркестратор + action-capable workers в отдельных окнах Chromium.

Hydra не «читает страницу и кликает по одному сценарию». Она строит **дерево действий**: главный агент планирует, а независимые подзадачи уходят в **параллельные воркеры**, каждый со своим Playwright-инстансом и полным набором действий (клик, ввод, навигация).

```bash
uv run hydra --demo "Найди на демо-стенде письма со словом «спам» и опиши, что с ними можно сделать"
```

---

## Чем Hydra отличается от типичного sequential-агента

В тестовых заданиях часто отсекают решения по трём осям. Hydra закрывает их явно.

| Критерий отклонения «обычного» агента | Как отвечает Hydra |
| --- | --- |
| **Слабое live-демо** на чужих сайтах | Видимый Chromium, persistent-профиль (логин руками), `--record-video`, демо-стенд и сценарии на реальных сайтах без зашитых селекторов |
| **Нет параллельных деревьев действий** | `parallel_delegate(tasks=[…])` → `ThreadPoolExecutor` + отдельный `sync_playwright` / `chromium.launch` на поток |
| **Read-only «субагенты»** | Воркеры **могут действовать**: click / type / select / navigate в своём окне; cookies наследуются через `storage_state` |

Опционально остаётся `delegate_reading` — read-only помощник **в общем** браузере оркестратора (для длинных списков без мутаций). Это дополнение, не замена воркерам.

---

## Архитектура

```
                    ┌─────────────────────────────┐
                    │     User / CLI (`hydra`)     │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │   AgentLoop (оркестратор)    │
                    │   SYSTEM + Toolbox (main)    │
                    │   persistent Chromium        │
                    │   profile / cookies / tabs   │
                    └──────┬───────────┬──────────┘
                           │           │
           parallel_delegate│           │ delegate_reading
                           │           │ (shared browser)
              ┌────────────▼──┐   ┌────▼────────────┐
              │ ThreadPool     │   │ Reader (RO)     │
              │ max_workers=N  │   │ snapshot/find/  │
              └─┬─────┬─────┬─┘   │ read/scroll     │
                │     │     │     └─────────────────┘
         ┌──────▼┐ ┌──▼───┐ ┌▼──────┐
         │Worker1│ │Worker2│ │WorkerN│
         │ own   │ │ own   │ │ own   │
         │Chromium│ │Chromium│ │Chromium│
         │ ACT   │ │ ACT   │ │ ACT   │
         └───┬───┘ └───┬───┘ └───┬───┘
             └─────────┴─────────┘
                   merged reports
```

**Важно для sync Playwright:** общий browser-context нельзя безопасно шарить между потоками. Поэтому параллелизм = **отдельный Playwright на поток**, а не «несколько Page в одном процессе».

Перед `parallel_delegate` оркестратор экспортирует `storage_state.json` из persistent-профиля — воркеры стартуют уже «залогиненными», если пользователь вошёл в главном окне.

---

## Быстрый старт

Требования: Python 3.11+, [uv](https://github.com/astral-sh/uv), ключ Anthropic (или совместимый endpoint).

```powershell
cd hydra
uv venv --python 3.11
uv pip install -e ".[dev]"
.\.venv\Scripts\playwright.exe install chromium

# если editable-install спотыкается о кириллицу в пути Windows:
$env:PYTHONPATH = (Resolve-Path src).Path

# если download Chromium таймаутится — можно работать через системный Chrome:
# $env:AGENT_BROWSER_CHANNEL = "chrome"

copy .env.example .env   # ANTHROPIC_API_KEY=...
uv run hydra --demo "Открой почту и перечисли темы входящих"
```

Полезные флаги:

| Флаг | Смысл |
| --- | --- |
| `--url` | Стартовая страница |
| `--demo` | Поднять `demo/site` на localhost |
| `--safety ask\|strict\|yolo` | Политика подтверждений |
| `--max-workers N` | Потолок параллельных воркеров |
| `--record-video` | Запись вкладок в `runs/<ts>/video/` |
| `--profile-dir` | Persistent-профиль Chromium |
| `--headless` | Скрыть окно (для CI, не для демо) |

Без задачи CLI входит в интерактивный REPL.

---

## Инструменты

### Браузер (оркестратор и воркеры)

`browser_snapshot`, `browser_find`, `browser_read_text`, `browser_click`, `browser_type`, `browser_select`, `browser_scroll`, `browser_press`, `browser_wait`, `browser_tabs`, `browser_navigate`, `browser_back`

### Мета

`note`, `ask_user`, `finish`

### Параллелизм и делегирование

| Инструмент | Кто | Что делает |
| --- | --- | --- |
| **`parallel_delegate`** | только main | Список `{goal, start_url?}` → N action-воркеров → объединённый отчёт |
| **`delegate_reading`** | только main | Read-only helper в **общем** окне |

Воркеры **не** получают `parallel_delegate` / `ask_user` / `delegate_reading` — они отрабатывают цель и зовут `finish`.

---

## Контекст и память

1. **Сжатие на входе** — семантический outline / diff вместо HTML.
2. **Pruning** — старые page-observation схлопываются в одну строку; пары tool_use / tool_result сохраняются.
3. **Compaction** — при превышении бюджета ранний хвост заменяется handoff-заметой модели.
4. **`note`** — факты вне транскрипта (цены, id, решения).

Контент страницы всегда в ограде `<page_content untrusted="true">` — это данные сайта, не инструкции.

---

## Безопасность

- Лексические правила (ru+en) по имени контроля + опциональный LLM-judge.
- Режимы: `ask` (по умолчанию), `strict`, `yolo`.
- Жёсткий блок паролей / номеров карт / CVV — даже в `yolo`.
- Воркеры без человека: medium/high **авто-отклоняются**, низкий риск проходит.

---

## MCP (опционально)

Тот же toolbox можно отдать внешнему клиенту:

```bash
uv pip install -e ".[mcp]"
python -m hydra.mcp_server
```

`parallel_delegate` / `ask_user` / `finish` в MCP не экспонируются — ими управляет агентный цикл.

---

## Сценарии для демо-видео

Снимайте **видимый** браузер + терминал (`--record-video` → `runs/<ts>/video/`). Скрипт `scripts/make_video.py` склеивает trace + запись, если установлен ffmpeg.

### 1. Параллельный разбор вакансий (hh.ru)

Задача в духе: «Сравни 2–3 вакансии Python / ML по зарплате и требованиям».  
Ожидание: оркестратор зовёт `parallel_delegate` с разными `start_url` / целями → несколько окон → сводный отчёт. Без захардкоженных селекторов.

### 2. Сравнение источников (Wikipedia + Habr)

«Кратко сравни определение темы X в энциклопедии и в технической статье».  
Параллельные воркеры читают разные домены; оркестратор мержит выводы.

### 3. Демо-стенд (без аккаунтов)

```powershell
uv run hydra --demo --safety yolo --record-video `
  "На почте отметь спам по теме про крипто, на доставке собери корзину из двух блюд — используй parallel_delegate"
```

Воспроизводимо, видно параллельные окна, подходит для CI-близких прогонов.

---

## Что намеренно НЕ захардкожено

Проверяется `scripts/check_no_hardcoding.py`:

- нет рецептов задач (`delete_spam`, `checkout`, …);
- нет site `data-qa` / test-id селекторов;
- нет URL/имён площадок в system prompt;
- словарь риска в `safety.py` только **гейтит**, не выбирает элементы.

Агент учится лексикону сайта из snapshot / find — как человек.

---

## Честные ограничения

- Sync Playwright → параллелизм дороже по RAM (отдельные Chromium).
- Воркеры не спрашивают пользователя: высокорисковые клики там режутся.
- Compact / judge зависят от API; без ключа полный live-прогон невозможен.
- Captcha, жёсткий bot-detection и 2FA остаются на человеке в главном окне.
- Качество на «живых» сайтах = качество модели + стабильность DOM, не магия селекторов.

---

## Тесты без API-ключа

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m pytest -q --timeout=60
```

Покрывают perception, safety, context, схемы воркеров, persistence профиля.

---

## Лицензия и назначение

Учебное / тестовое задание: автономный AI browser agent с упором на **параллельные action-деревья**. Пакет: `hydra-browser`, CLI: `hydra`.
