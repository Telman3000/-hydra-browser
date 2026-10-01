# Hydra

**Параллельный браузерный агент:** оркестратор + action-capable воркеры, каждый в своём окне Chromium.

Hydra не следует сценарию. Главный агент сам смотрит на страницу, решает, что делать дальше, и, когда задача распадается на независимые части, раздаёт их **параллельным воркерам**. У каждого воркера свой Playwright и полный набор действий: клик, ввод, навигация.

## Живое демо: hh.ru, три параллельных воркера

```powershell
uv run hydra --url https://hh.ru --record-video "Найди на hh.ru 3 свежие вакансии AI-инженера / Python LLM-разработчика (Москва или удалёнка). Собери ссылки на них со страницы поиска, затем изучи все 3 параллельно: компания, зарплата, требуемый опыт, ключевой стек, и для каждой напиши короткое сопроводительное под мой профиль: Python, LLM-агенты, Playwright, RAG. Не откликайся. Итог - таблица + сопроводительные."
```

Видео: [`demo/demo-hh-parallel.mp4`](demo/demo-hh-parallel.mp4) (72 с). Слева терминал со всеми tool calls, справа браузер. Пока работает `parallel_delegate`, справа сетка из окон воркеров.

![Три воркера параллельно разбирают вакансии на hh.ru](demo/hh-parallel-grid.png)

Что произошло в этом прогоне (`gpt-4.1-mini`, 8 шагов оркестратора, ~60 с, ~$0.02):

1. Оркестратор сам составил поиск, нашёл вакансии через `browser_find` и взял их адреса из `href` ссылок.
2. `parallel_delegate` запустил 3 воркера в отдельных Chromium. Каждый открыл свою вакансию, прочитал её и вернул отчёт с сопроводительным.
3. Оркестратор свёл отчёты в таблицу. Откликов не было: так просили в задаче, и safety gate всё равно не дал бы воркеру нажать «Откликнуться».

![Итоговый отчёт](demo/hh-result.png)

Второе видео, [`demo/demo-mail.mp4`](demo/demo-mail.mp4), снято на локальном демо-стенде: почта, удаление спама с подтверждением.

---

## Чем Hydra отличается от типичного sequential-агента

| Типичная слабость | Как это решено в Hydra |
| --- | --- |
| **Нет live-демо** на чужом сайте | Демо выше снято на настоящем hh.ru без единого зашитого селектора. `--record-video` + `scripts/make_video.py` собирают ролик «терминал + браузер» прямо из трейса прогона |
| **Один браузер, всё по очереди** | `parallel_delegate(tasks=[…])` → `ThreadPoolExecutor`, в каждом потоке свой `sync_playwright()` + `chromium.launch()` |
| **Субагенты только читают** | Воркеры **действуют**: click / type / select / navigate в своём окне. Cookies передаются им через `storage_state` |

Дополнительно есть `delegate_reading`: read-only помощник в **общем** окне оркестратора для длинных списков, где ничего не нужно менять.

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

**Про sync Playwright:** один browser context нельзя безопасно делить между потоками. Поэтому параллельность устроена как **отдельный Playwright на каждый поток**, а не как несколько Page в одном процессе.

Перед `parallel_delegate` оркестратор экспортирует `storage_state.json` из persistent-профиля. Если пользователь залогинился в главном окне, воркеры стартуют уже залогиненными.

---

## Быстрый старт

Требования: Python 3.11+, [uv](https://github.com/astral-sh/uv), ключ OpenAI или Anthropic.

```powershell
cd hydra
uv venv --python 3.11
uv pip install -e ".[dev,video]"
.\.venv\Scripts\playwright.exe install chromium   # или системный Chrome, см. ниже

# если editable-install спотыкается о кириллицу в пути Windows:
$env:PYTHONPATH = (Resolve-Path src).Path

copy .env.example .env   # AGENT_PROVIDER=openai + OPENAI_API_KEY=...  (или ANTHROPIC_API_KEY)
uv run hydra --demo "Открой почту и перечисли темы входящих"
```

Если загрузка Chromium падает по таймауту, задайте `AGENT_BROWSER_CHANNEL=chrome` в `.env`, и агент будет работать через установленный Google Chrome.

Модели по умолчанию: `gpt-4.1-mini` для OpenAI, `claude-sonnet-4-5` для Anthropic. Переопределяются через `AGENT_MODEL` или `--model`.

| Флаг | Смысл |
| --- | --- |
| `--url` | Стартовая страница |
| `--demo` | Поднять `demo/site` на localhost |
| `--safety ask\|strict\|yolo` | Политика подтверждений |
| `--max-workers N` | Потолок параллельных воркеров |
| `--record-video` | Запись главного окна и окон воркеров в `runs/<ts>/` |
| `--profile-dir` | Persistent-профиль Chromium |
| `--headless` | Скрыть окна (для CI, не для демо) |

Без задачи CLI входит в интерактивный REPL.

### Видео «терминал + браузер»

```powershell
python scripts/setup_video.py        # один раз: отдаёт Playwright системный ffmpeg (CDN Playwright часто недоступен)
uv run hydra --record-video --url https://hh.ru "…задача…"
python scripts/make_video.py runs/<ts> -o demo.mp4
```

`make_video.py` берёт из `trace.jsonl` каждый tool call с таймстемпом и рисует по нему панель терминала. Рядом ставится запись браузера, а пока работают воркеры, на этом месте сетка их окон. Так ролик синхронизирован с реальным прогоном без ручного монтажа.

---

## Инструменты

### Браузер (оркестратор и воркеры)

`browser_snapshot`, `browser_find`, `browser_read_text`, `browser_click`, `browser_type`, `browser_select`, `browser_check`, `browser_scroll`, `browser_press`, `browser_wait`, `browser_tabs`, `browser_navigate`, `browser_back`, `browser_screenshot`

### Мета

`note`, `ask_user`, `finish`

### Параллелизм и делегирование

| Инструмент | Кто | Что делает |
| --- | --- | --- |
| **`parallel_delegate`** | только main | Список `{goal, start_url?}` → N action-воркеров → объединённый отчёт |
| **`delegate_reading`** | только main | Read-only помощник в **общем** окне |

Воркерам недоступны `parallel_delegate`, `ask_user` и `delegate_reading`: они выполняют свою цель и вызывают `finish`.

---

## Как агент видит страницу

HTML модели не передаётся. `browser_snapshot` возвращает семантический outline с хэндлами:

```
- link "AI/ML Engineer (LLM/RAG)" (href=/vacancy/137780796) [e37]
- combobox "Профессия, должность или компания" [e16]
- button "Найти" [e18]
```

После действия модель получает **diff** вместо полной страницы. У ссылок виден `href`, поэтому оркестратор может раздать воркерам реальные адреса.

## Контекст и память

1. **Сжатие на входе:** семантический outline или diff вместо HTML.
2. **Pruning:** старые наблюдения страницы схлопываются в одну строку, пары tool_use / tool_result сохраняются.
3. **Compaction:** при превышении бюджета ранняя часть истории заменяется handoff-заметкой модели.
4. **`note`:** факты вне транскрипта (цены, id, решения).

Контент страницы всегда обёрнут в `<page_content untrusted="true">`: это данные сайта, а не инструкции.

---

## Безопасность

- Лексические правила (ru+en) по имени элемента плюс опциональный LLM-judge.
- Режимы: `ask` (по умолчанию), `strict`, `yolo`.
- Пароли, номера карт и CVV блокируются жёстко, даже в `yolo`.
- У воркеров нет человека, поэтому действия риска medium/high **автоматически отклоняются**, низкий риск проходит.

---

## MCP (опционально)

Тот же toolbox можно отдать внешнему клиенту:

```bash
uv pip install -e ".[mcp]"
python -m hydra.mcp_server
```

`parallel_delegate`, `ask_user` и `finish` через MCP не экспонируются: ими управляет агентный цикл.

---

## Что намеренно НЕ захардкожено

Проверяется скриптом `scripts/check_no_hardcoding.py`:

- нет рецептов задач (`delete_spam`, `checkout`, …);
- нет селекторов `data-qa` / test-id под конкретные сайты;
- нет URL и названий площадок в system prompt;
- словарь риска в `safety.py` только **гейтит** действия и не выбирает элементы.

Лексикон сайта агент узнаёт из snapshot и find, как это делал бы человек.

---

## Честные ограничения

- Sync Playwright делает параллельность дороже по RAM: на каждого воркера отдельный Chromium.
- Воркеры не спрашивают пользователя, поэтому высокорисковые клики у них отсекаются.
- Нужен ключ API: без него полный live-прогон невозможен, тесты работают и так.
- Captcha, жёсткий bot-detection и 2FA остаются на человеке в главном окне.
- Модели уровня mini иногда пишут цели воркерам по-английски, хотя задача на русском. Итоговый отчёт всё равно на языке пользователя.

---

## Тесты без API-ключа

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m pytest -q --timeout=120
```

58 тестов: perception (outline, diff, href), safety, context, адаптер OpenAI, агентный цикл на scripted FakeLLM, два настоящих параллельных Chromium-воркера, MCP-сервер, persistence профиля.

---

Тестовое задание: автономный AI browser agent с упором на **параллельные action-деревья**. Пакет `hydra-browser`, CLI `hydra`.
