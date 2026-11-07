# Claude r4 — 2026-11-07

**Verdict: Blockers** (один)
**Tip reviewed:** `583c1d50a318ad554cc03891ea2a22895836903a` (`cursor/mvp-cli-corpus-dafb`, PR #2)
**Prior tip / report:** `0eb867d2996183a09818b3f4a5ff5459a8534dbb` / `docs/reviews/claude-r3.md`. Отчёты и пробы r1–r3 в диффе не изменены, проверено.
**Scope:** дифф `0eb867d..583c1d5` (1 коммит): `metria/adapters/{structural,supply_chain,test_adequacy}.py`, `metria/{discovery,runner,sandbox}.py`, `tests/test_r2_blockers.py`, `tests/test_r3_blockers.py` (новый), перегенерированный `corpus/results/*`.
**Прогнано:** `pytest -q` → 2 failed, 40 passed (причина в моей офлайн-среде, см. N-r4-1); все пробы r1 (6), r2 (4), r3 (3); 2 новые пробы в `docs/reviews/claude-r4-repro/`; полный повторный прогон корпуса на Linux / CPython 3.13.18 (pip и `api.osv.dev` из моей среды недоступны).

---

## Итог по r3

| r3 | Статус в r4 | Доказательство |
|---|---|---|
| **B1** scrub голого `/tmp`, красный тест на Linux | **Closed** | `test_n_r2_3_scrub_linux_tmp_paths` и `test_r3_b1_*` зелёные на Linux; `/tmpfoo/bar` не трогается; `probe_linux_tmp_paths.sh` → `manifest identical: yes`; `probe_determinism.sh` → всё `True`. Остаток: нескрабленные строки ошибок → N-r4-4 |
| **B2** `--no-tests` → `pip install -e .` | **Closed** | `probe_no_tests_runs_subject_build.py` → ни одного editable install при `run_tests=False`; `allow_subject_install=run_tests` в runner |
| **B3** чужой `ModuleNotFoundError` в теле теста → fail | **Closed** для flat-layout | `probe_third_party_import_in_test_body.py` → `null / test_env_unavailable / classify: env`. Кейсы r2 A/B остаются `fail` (`probe_env_heuristic_hides_fail.py`). Но атрибуция «свой/чужой модуль» ломается на src-layout → новый **B1** |
| N-r3-1 `PYTHONUSERBASE` | **Closed** | `PYTHONNOUSERSITE=1`, `PYTHONUSERBASE` нет (юнит-тест + `probe_env_leak_run_mode.py`) |
| N-r3-2 `==1.*` как пин | **Closed** | `probe_wildcard_pin.py` → `spec='==1.*' pinned=None` |
| N-r3-3 вердикты зависят от даты резолва, не раскрыто | **Not addressed**, в digest не указан как deferred | Нет `pip freeze` тестового venv, даты резолва в отчёте, флага сети раннера |
| N-r3-4 второй venv для резолва | **Not addressed** | `resolve_runtime_versions` по-прежнему строит отдельный venv |
| N-r3-5 S101 в тестах | **Partially closed** → N-r4-5 | flask 38 → 5, click 14 → 10; six по-прежнему 24 (все в корневом `test_six.py`) |
| N-r3-6 CI / wall_clock / vendor | **CI явно deferred**; `wall_clock_s` и vendor/generated не тронуты и не перечислены | `.github/` нет; `runner.py:189` только пишет `wall_clock_s` |

**r1 / r2 (повторно):** все 10 проб дают ожидаемый результат — env-leak `--no-tests` и run-режим, OSV fail-closed, `re.compile`, worktree, URL userinfo, determinism, классификатор путей, Linux tmp.

---

## Blockers

### B1. src-layout с именем импорта ≠ имени дистрибутива: сломанный внутренний импорт субъекта → `NOT MEASURED` (класс r2 B1)

`_subject_module_names()` смотрит только на верхний уровень корня (каталог `src/` без `.py` пропускается) плюс на regex `name = …` из `pyproject.toml`/`setup.cfg`. Для src-layout пакет субъекта распознаётся, только если **имя дистрибутива совпадает с именем импорта**. Если они различаются (самый массовый случай: Pillow/`PIL`, PyYAML/`yaml`, beautifulsoup4/`bs4`, scikit-learn/`sklearn`), `ModuleNotFoundError: No module named 'imgkit.helperz'` считается сторонним модулем, и **реальный дефект субъекта** уходит в `NOT MEASURED / test_env_unavailable / metria`. Null не участвует в `overall`, поэтому сломанный продукт получает нейтральный итог. Это ровно режим отказа r2 B1, только для src-layout; в корпусе так устроены 3 из 5 субъектов (click, flask, requests), сейчас их спасает совпадение имён.

**Repro:** `python3 docs/reviews/claude-r4-repro/probe_src_layout_internal_import.py` (venv-стаб как в r2/r3; опечатка во внутреннем импорте `src/imgkit/core.py`):
```
src layout, dist name == import name     -> status=fail null_reason=None subject_names=['imgkit']
src layout, dist name != import name     -> status=null null_reason=test_env_unavailable subject_names=['Imaging_Kit']
```

**Закрыть:** определять пакеты субъекта по дереву, а не по имени дистрибутива. Все каталоги с `__init__.py` и модули `*.py` на верхнем уровне **и** под `src/`, плюс явные `packages`/`package-dir`/`[tool.setuptools.packages.find]`, если они есть. Самый надёжный вариант: модуль принадлежит субъекту, если `<root>/<mod>` или `<root>/src/<mod>` существует как пакет или модуль. Тест: проба выше → `fail` в обоих кейсах.

---

## Nits (code-fixable; по playbook закрыть до r5)

- **N-r4-1. Юнит-тесты требуют PyPI, а регрессионный тест r3 B3 проходит вхолостую.** `_stub_venv_with_pytest` теперь делает `pip install pytest` в чистом venv (`check=True`). Офлайн это бросает `CalledProcessError`, `run_test_adequacy` ловит его как `venv_failed`, и линза возвращает `NOT MEASURED`. Последствия: `test_b1_broken_subject_import_is_fail` и `test_b1_assertion_plus_importerror_text_is_fail` красные, а `test_r3_b3_third_party_import_in_test_body_is_env` **зелёный по неправильной причине** (ждёт `null` и получает `null` от `venv_failed`, а не от классификации). Мои пробы r1–r3 со стабом `--system-site-packages` показывают, что код верен, — но тесты этого не доказывают. Нужно: стаб без сети (`--system-site-packages`: system site не зависит от `PYTHONNOUSERSITE`, это не user-site оператора) и в r3_b3 проверять `raw_metrics["classify"] == "env"` и `suite == "env_error"`, а не только `status`.
- **N-r4-2. `supply_chain: WARN`, когда не измерено ничего.** Если ни одна runtime-версия не резолвнута, `osv_queries = 0`, и единственные находки — «Could not resolve version … (adapter_gap)». Линза при этом ставит `warn`: пробел Metria выдаётся за статус субъекта. На повторном прогоне httpx: `warn`, `osv_queries=0`, 5 adapter_gap. В опубликованном корпусе click `warn` целиком из-за двух нерезолвнутых маркерных зависимостей (см. N-r4-3). Нужно: `osv_queries == 0 and unresolved > 0` → `null / adapter_gap / null_owner=metria`; частичный резолв → статус по измеренной части + `confidence: low`.
- **N-r4-3. Зависимости, исключённые маркером среды, опрашиваются по нижней границе диапазона.** `importlib-metadata>=3.6.0; python_version < '3.10'` на 3.13 не ставится, и `range_fallback` спрашивает OSV про `importlib-metadata==3.6.0` — версию, которую никто не устанавливает. Так можно получить CVE (вплоть до critical → hard gate) на пакет, которого в окружении нет. Repro: `python3 docs/reviews/claude-r4-repro/probe_marker_dep_lower_bound.py` → `OSV queried: ['importlib-metadata==3.6.0', 'werkzeug==3.1.9'] | range_fallback: ['importlib-metadata']`. Вычислять маркеры (`packaging.markers.Marker(...).evaluate()`): не применимо → `not_applicable` без запроса и без adapter_gap. Нижнюю границу как «резолв» не использовать: зависимость, которой нет в `pip list`, — это `unresolved`.
- **N-r4-4. Строки ошибок не проходят scrub.** `resolved_versions.json → meta.errors` (stderr pip) и `raw_metrics.error`/`resolve` в `stress-run.json` пишутся как есть. На Linux в офлайн-прогоне корпуса там оказывается `file:///tmp/metria-corpus-httpx-nazkfegf` (все 5 субъектов). Опубликованный mac-корпус чистый, потому что там pip не падал. Прогонять через `scrub_text_paths` любую строку, которая попадает в evidence или `raw_metrics`.
- **N-r4-5. S101: тесты в корне репозитория не распознаются.** `is_test_fixture_path("test_six.py")` → `False` (сегмент `test_*` учитывается только как каталог). У six все 24 оставшихся S101 — из `test_six.py`. Для фильтра S101 нужен отдельный предикат «тестовый файл»: `test_*.py`, `*_test.py`, `conftest.py` + тестовые каталоги. Попутно: `rel` в заголовках ruff-находок = `<subject>/src/…`, потому что `filename` скрабится **до** `relative_to`. Это косметика, но fingerprints несут префикс `<subject>/`; считать `rel` до scrub.
- **N-r4-6 (= N-r3-3, N-r3-4, не тронуты и не в deferred).** Раскрытие даты резолва и `pip freeze` тестового venv в evidence, флаг сети раннера, сетевые тесты (requests `TestTimeout`); переиспользование test-venv вместо второго резолв-venv.
- **N-r4-7 (deferred / открыто).** CI deferred (причина — scope токена на Mac; workflow можно добавить через веб-интерфейс GitHub). Красные офлайн-тесты N-r4-1 — ещё один аргумент: job без сети к PyPI сразу показал бы, что тесты сетезависимы. `wall_clock_s` не соблюдается; vendor/generated не детектируются.

---

## What I did not check

- **macOS / CPython 3.9.6** (профиль опубликованного корпуса) — у меня Linux / 3.13.18.
- **Юнит-тесты с доступом к PyPI**: `test_b1_*` (r2) и `test_r3_b3_*` с сетью не прогонял. Корректность кода подтверждаю пробами со стабом без сети, а не этими тестами.
- **Живой OSV** (заблокирован) и реальные хиты опубликованного корпуса (httpx `anyio==4.12.1` critical, requests `urllib3==2.6.3`).
- **Успешная установка зависимостей субъекта** и фактическое исполнение `setup.py` — pip офлайн.
- **Windows.**
- **`metri/docs/claude-r3-fix-digest.md`, `metri/docs/claude-r4-ping.md`** — в репозитории их нет; сверял по коду и пингу.

## Probes / repro

Из корня репозитория.

r1–r3 (перепрогнаны на этом tip, все ожидаемые):
```bash
bash    docs/reviews/claude-r1-repro/probe_env_leak.sh
python3 docs/reviews/claude-r1-repro/probe_osv_fail_open.py
python3 docs/reviews/claude-r1-repro/probe_recompile.py
bash    docs/reviews/claude-r1-repro/probe_local_sha_mutates.sh
python3 docs/reviews/claude-r1-repro/probe_parse_target.py
bash    docs/reviews/claude-r1-repro/probe_determinism.sh
python3 docs/reviews/claude-r2-repro/probe_env_heuristic_hides_fail.py
python3 docs/reviews/claude-r2-repro/probe_fixture_classifier.py
python3 docs/reviews/claude-r2-repro/probe_env_leak_run_mode.py
bash    docs/reviews/claude-r2-repro/probe_linux_tmp_paths.sh
python3 docs/reviews/claude-r3-repro/probe_no_tests_runs_subject_build.py
python3 docs/reviews/claude-r3-repro/probe_third_party_import_in_test_body.py
python3 docs/reviews/claude-r3-repro/probe_wildcard_pin.py
```

r4 (новые):
```bash
python3 docs/reviews/claude-r4-repro/probe_src_layout_internal_import.py  # B1 → оба кейса status=fail
python3 docs/reviews/claude-r4-repro/probe_marker_dep_lower_bound.py      # N-r4-3 → importlib-metadata не опрошен
python3 scripts/run_corpus.py --out "$PWD/.rerun"   # офлайн: httpx supply_chain → NOT MEASURED, grep '/tmp/metria-' пуст
```
