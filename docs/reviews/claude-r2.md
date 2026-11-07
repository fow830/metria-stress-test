# Claude r2 — 2026-11-06

**Verdict: Blockers**
**Tip reviewed:** `14d486dcffdbcd6829c11bec53e2bd308c38c0bd` (`cursor/mvp-cli-corpus-dafb`, PR #2)
**Prior tip / report:** `ee889648d12678dbd1546db1d5184da190bb3788` / `docs/reviews/claude-r1.md` (не изменён в этом диффе, проверено)
**Scope:** весь дифф `ee88964..14d486d` (1 коммит, 84 файла): `metria/sandbox.py` (новый), все адаптеры, `discovery`, `cli`, `runner`, `model`, `evidence`, `scripts/run_corpus.py`, policy, pyproject, README, `tests/test_r1_blockers.py`, все 5 перегенерированных `corpus/results/*` + `RUNNER_PROFILE.json`.
**Прогнано:** `pytest -q` → **28 passed**; все 6 проб r1; 4 новые пробы в `docs/reviews/claude-r2-repro/`; полный повторный прогон корпуса на Linux / CPython 3.13.18 (pip и `api.osv.dev` из моей среды недоступны — это само по себе проверяет B2/B3-атрибуцию).

---

## Итог по r1

| r1 | Статус в r2 | Доказательство |
|---|---|---|
| **B1** код субъекта с env оператора | **Closed** (в пределах минимума r1) | `probe_env_leak.sh`: conftest под `--no-tests` не исполняется. Новая `probe_env_leak_run_mode.py`: в run-режиме субъект видит `secret_like_env: []`, `HOME` подменён. Остатки → N-r2-1 |
| **B2** OSV fail-open | **Closed** | `probe_osv_fail_open.py` → `status: null, osv_errors: 2`; повторный корпус: httpx `supply_chain` → `NOT MEASURED` (не PASS) |
| **B3** Adequacy мерит раннер; атрибуция | **Partially closed → новый B1** | Направление исправлено: venv + установка, env-сбои → `NOT MEASURED/metria`. Но эвристика перекорректирована и прячет **настоящие** падения субъекта (см. B1 ниже); установка test-deps по сути не работает (N-r2-2) |
| **B4** воспроизводимость, окружение | **Closed** с остатком | `runner_environment.json`, `RUNNER_PROFILE.json`, `runner_image_digest`-тег, ruff ищется и в PATH, `ruff absent` понижает confidence. Повторный прогон расходится только честными `NOT MEASURED` моей офлайн-среды. Остаток: на Linux пути не вычищаются → N-r2-3 |
| **B5** чужие CVE на субъекте | **Closed** | Flask больше не репортит `flask==2.3.2` из `examples/`; классификация `runtime/dev/docs/example/test`; `pyproject`/`setup.cfg` парсятся; severity из `database_specific`/CVSS, без дефолта `high`. Остатки → N-r2-4, N-r2-5 |
| **B6** hard gate на тестовых ключах | **Partially closed → новый B2** | requests больше не FAIL, ключи помечены `Test-fixture`. Но классификатор матчит подстроку и пропускает настоящие ключи мимо hard gate (B2 ниже) |

**r1 nits:** N-1 ✓ (`probe_recompile.py` → `pass []`), N-2 ✓ (`--isolated`, filter→cap; новая проблема шума → N-r2-6), N-3 ✓ (`probe_local_sha_mutates.sh` → ветка остаётся `master`, через `git worktree`), N-4 ✓ (`probe_parse_target.py`), N-5 ✓, N-6 ✓ для zip (unit-тест) / ✗ для manifest на Linux (N-r2-3), N-7 ✓, N-8 частично (`--profile` предупреждает, мёртвый код убран; **`wall_clock_s` всё ещё не соблюдается**, только пишется), N-9 частично (`excluded_paths` реальные, `supply_chain.loc_analyzed=0`; vendor/generated по-прежнему не детектируются), N-10 частично (`FormatChecker` ✓; **CI нет** — `.github/` отсутствует), N-11 ✓, N-12 ✓ по статусу, но см. N-r2-5.

---

## Blockers

### B1. Эвристика env-сбоя прячет настоящие падения субъекта под `NOT MEASURED` (fail-open в обратную сторону)

`_looks_like_env_failure()` возвращает `True`, если **где угодно** в выводе pytest встречается `ImportError|ModuleNotFoundError|No module named|ERROR collecting|…` (плюс захардкоженные под корпус `pytest-httpbin`, `_dbm`), или exit ∈ {2,3,4}, или есть `ERROR` без `FAILED`. Проверка стоит **перед** классификацией FAIL, поэтому любое упоминание превращает весь прогон в `null / test_env_unavailable / metria`. Поскольку null не участвует в `overall`, сломанный продукт может получить итог `pass`/`warn`.

r1 просил: setup/import/fixture-ошибки окружения → NOT MEASURED, assertion-провалы → `fail`. Сейчас субъекту прощается всё, что похоже на импорт, включая его собственные дефекты.

**Repro:** `python3 docs/reviews/claude-r2-repro/probe_env_heuristic_hides_fail.py` (venv с system site-packages, установка застаблена; остальной код настоящий):
```
A broken internal import                 -> status=null null_reason=test_env_unavailable display=NOT MEASURED
B real assertion + 'ImportError' text    -> status=null null_reason=test_env_unavailable display=NOT MEASURED
```
A — у субъекта опечатка во внутреннем импорте (`from pkg.helperz import …`): это дефект продукта, а не раннера. B — настоящий `assert 1 + 1 == 3` плюс соседний тест, в тексте ошибки которого есть слово `ImportError`.

**Закрыть:**
1. Классифицировать **по тестам**, а не по всему тексту: запускать с `--junitxml` (или `-rfE` + разбор short summary) и смотреть тип исключения у каждого failure/error.
2. Env-сбой = `ModuleNotFoundError`/`ImportError`, где отсутствующий модуль **не принадлежит субъекту** (имя не резолвится в его дерево/пакеты), + `fixture '…' not found`, + ошибки collection до первого теста. Отсутствующий модуль самого субъекта → дефект субъекта.
3. Смешанный случай: есть хотя бы один assertion-провал (или исключение субъекта) → `fail`; env-ошибки идут отдельной находкой и снижают `confidence`.
4. Убрать захардкоженные `pytest-httpbin|_dbm` из регэкспа — это подгонка под корпус.
5. Тесты на A и B (ожидание: `fail`), плюс текущие env-кейсы (ожидание: `NOT MEASURED`).

### B2. Классификатор тестовых путей матчит подстроку — настоящие ключи уходят мимо единственного hard gate

`is_test_fixture_path()` проверяет `any(m in low for m in ("tests/", "test/", "testdata/", "test_data/", "fixtures/", "conftest"))` как **подстроку**. `"test/"` содержится в `latest/`, `attest/`, `contest/`, `pytest/`, `greatest/` и т.д. Найденный там приватный ключ понижается до `medium` «Test-fixture key material», hard gate не срабатывает. Это единственный hard gate structural-линзы, и он обходится выбором имени каталога.

**Repro:** `python3 docs/reviews/claude-r2-repro/probe_fixture_classifier.py`:
```
deploy/latest/server.key         fixture=True
infra/attest/signing.pem         fixture=True
src/contest/key.pem              fixture=True
tests/certs/server.key           fixture=True
real deploy key -> status: warn | hard_gate_failures: []
```

**Закрыть:** матчить по **компонентам пути** (`parts = rel.split("/")`; `parts[i] in {"tests","test","testdata","test_data","fixtures"}` или `parts[i].startswith("test_")`, файл `conftest.py`), без подстрок. Тест: `deploy/latest/server.key` → hard gate; `tests/certs/server.key` → без hard gate.

---

## Nits (code-fixable; по playbook закрыть до r3)

- **N-r2-1. Остатки B1.** (a) `PYTHONPATH` и `PYTHONHOME` оператора проходят в env субъекта: проба показала `PYTHONPATH: /operator/injected/path`. Это не секрет, но канал подмены импорта и утечки путей — убрать из allowlist. (b) README пишет «operator tokens/keys are not inherited». Это верно для env, но не для файлов: субъект читает `~/.config/gh/hosts.yml`, `~/.aws/credentials`, `~/.ssh/*` по абсолютному пути (проба: `can_read_operator_home_by_abs_path: true`). Сформулировать честно: «env vars scrubbed; filesystem is not isolated — run on a throwaway machine/container». Контейнер — до Action/hosted, как договорено в r1.
- **N-r2-2. Установка test-deps почти не работает** (причина 3 из 5 `NOT MEASURED` в опубликованном корпусе). `install_subject_test_deps` пробует `.[test]` первым; pip на **несуществующий** extra отвечает `WARNING: requests 2.32.3 does not provide the extra 'test'` с кодом 0, цикл делает `break`, и `requirements-dev.txt` (там `pytest-httpbin`, `trustme`) не ставится никогда. В `install.json` при этом записано `installed: ["pytest", "-e .[test]"]` — запись вводит в заблуждение. Repro: `corpus/results/requests@61e2240f283f/evidence/test_adequacy/install.json` (`stderr_tail`). Читать extras из метаданных (`pyproject [project.optional-dependencies]` / `setup.cfg [options.extras_require]`), ставить только существующие, после editable **дополнительно** ставить `requirements*{test,dev}*.txt`; предупреждение pip о missing extra считать неуспехом.
- **N-r2-3. Пути Linux не вычищаются → evidence непереносимо и manifest недетерминирован на Linux.** `scrub_text_paths` знает только macOS `/var/folders/…`, а `/tmp/metria-adeq-*/venv/bin/python` остаётся в `collect.txt`/`collect_failed.txt`. Repro: `bash docs/reviews/claude-r2-repro/probe_linux_tmp_paths.sh` → `manifest identical: NO`, `/tmp/metria-adeq-8v60pk2h`. Проба r1 `probe_determinism.sh` теперь тоже даёт `manifest_sha256 identical: False` (в r1 было `True`) — регрессия. Вычищать по известным корням (`work`, `venv`, `home`, `tempfile.gettempdir()`), а не по OS-специфичному регэкспу; тест на Linux-путь.
- **N-r2-4. CVE-проверка на корпусе фактически выключена.** В опубликованном корпусе `osv_queries` = 0, 0, 0, 1, 0: библиотеки корректно объявляют диапазоны (`>=`), а OSV опрашивается только для `==`. Каждая библиотека получает `supply_chain: WARN` за «Unpinned runtime dependency». Для библиотеки диапазон — правильная практика, а не находка. Брать резолвленные версии из venv, где зависимости уже поставлены (`pip list --format json`) и проверять их; если venv нет — `adapter_gap` по пакету, без WARN за саму практику.
- **N-r2-5. click: «No runtime dependencies declared (components=0)» — ложное утверждение.** click 8.1.7 объявляет `install_requires` в `setup.py` (`colorama; platform_system == 'Windows'`, `importlib-metadata; python_version < '3.8'`), а `setup.py` не парсится. Нельзя писать «не объявлено», если источник не прочитан: `setup.py` есть, а статически не разобран → `adapter_gap` / `NOT MEASURED` по inventory.
- **N-r2-6. Шум ruff `S101` («Use of `assert`») в тестах.** `--isolated --select S,…` включает S101, и он флудит structural: flask 38, httpx 26, click 14. Assert в тестах — это и есть тест. Исключить S101 (или все S-правила) на тестовых путях (по компонентам пути, см. B2).
- **N-r2-7 (из r1, открыто).** `budget.wall_clock_s` не соблюдается (`runner.py:189` только записывает). Нет CI: `.github/workflows` отсутствует, PR по-прежнему проверяется только локальным прогоном. Vendor/generated не детектируются (N-9).

---

## What I did not check

- **macOS / CPython 3.9.6** — профиль опубликованного корпуса (`RUNNER_PROFILE.json`). У меня только Linux / 3.13.18; идентичность статусов на том же профиле не проверена. Ветка `tomli` для Python < 3.11 не проверена.
- **Успешная установка зависимостей субъекта:** pip в моей среде офлайн, поэтому путь «зависимости поставились → тесты прошли/упали» я гонял только через стаб (`--system-site-packages`) в пробах B1/N-r2-1. Реальная установка проверена только по опубликованным `install.json`.
- **Живой OSV:** `api.osv.dev` заблокирован egress-прокси. `severity_from_osv` проверен по коду и юнит-тестам, на реальных ответах нет.
- **Windows** (`Scripts/python.exe`-ветка в `create_subject_venv`).
- **GitHub Actions** — workflow нет (N-r2-7).

## Probes / repro

Из корня репозитория.

r1 (перепрогнаны на этом tip):
```bash
bash    docs/reviews/claude-r1-repro/probe_env_leak.sh           # B1 → leaked.txt не создан ✓
python3 docs/reviews/claude-r1-repro/probe_osv_fail_open.py     # B2 → status: null ✓
python3 docs/reviews/claude-r1-repro/probe_recompile.py         # N-1 → pass [] ✓
bash    docs/reviews/claude-r1-repro/probe_local_sha_mutates.sh # N-3 → after: master ✓
python3 docs/reviews/claude-r1-repro/probe_parse_target.py      # N-4 ✓
bash    docs/reviews/claude-r1-repro/probe_determinism.sh       # N-6 → zip ✗ / manifest ✗ на Linux (N-r2-3)
```

r2 (новые):
```bash
python3 docs/reviews/claude-r2-repro/probe_env_heuristic_hides_fail.py # B1 → оба кейса должны стать fail
python3 docs/reviews/claude-r2-repro/probe_fixture_classifier.py       # B2 → latest/attest/contest = fixture=False, hard gate
python3 docs/reviews/claude-r2-repro/probe_env_leak_run_mode.py        # N-r2-1 → PYTHONPATH None; README честно про FS
bash    docs/reviews/claude-r2-repro/probe_linux_tmp_paths.sh          # N-r2-3 → manifest identical: yes
python3 scripts/run_corpus.py --out "$PWD/.rerun"                       # сверка с corpus/results/SUMMARY.json
```
