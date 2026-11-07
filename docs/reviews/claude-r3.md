# Claude r3 — 2026-11-07

**Verdict: Blockers**
**Tip reviewed:** `0d34fb83f60c849b719568c4b9ddbf9d3856a92e` (`cursor/mvp-cli-corpus-dafb`, PR #2)
**Prior tip / report:** `d8d775bceee462d87104ba746873249b29a63bfe` / `docs/reviews/claude-r2.md`. Отчёты и пробы r1/r2 в диффе не изменены, проверено.
**Scope:** дифф `d8d775b..0d34fb8` (2 коммита: `ebd4de1`, `0d34fb8`): `metria/adapters/test_adequacy.py`, `metria/adapters/supply_chain.py`, `metria/discovery.py`, `metria/sandbox.py`, README, `tests/test_r2_blockers.py`, все 5 перегенерированных `corpus/results/*`.
**Прогнано:** `pytest -q` → **1 failed, 37 passed** (Linux); все 6 проб r1 и 4 пробы r2; 3 новые пробы в `docs/reviews/claude-r3-repro/`; полный повторный прогон корпуса (Linux / CPython 3.13.18; pip и `api.osv.dev` из моей среды недоступны).

---

## Итог по r2

| r2 | Статус в r3 | Доказательство |
|---|---|---|
| **B1** эвристика прячет падения субъекта | **Closed** для обоих кейсов r2 | `probe_env_heuristic_hides_fail.py` → A и B: `status=fail`. Junit-классификация + принадлежность модуля субъекту. Обратный кейс (env-падение в теле теста) теперь даёт `fail` → новый **B3** |
| **B2** классификатор путей по подстроке | **Closed** | `probe_fixture_classifier.py` → `latest/attest/contest` = `fixture=False`, `deploy/latest/server.key` → hard gate; `tests/certs` → fixture |
| N-r2-1 PYTHONPATH / README про FS | **Closed** (с регрессией → N-r3-1) | `probe_env_leak_run_mode.py` → `PYTHONPATH: null`, `secret_like_env: []`; README честно пишет про неизолированную ФС |
| N-r2-2 install останавливался на missing extra | **Closed** | Только объявленные extras; missing-extra не считается успехом; requests: `-e .` + `-r requirements-dev.txt` поставлены (опубликованный `install.json`) |
| N-r2-3 Linux tmp-пути | **Not closed на Linux** → **B1** | Тест самого PR `test_n_r2_3_scrub_linux_tmp_paths` красный на Linux |
| N-r2-4 CVE-проверка выключена | **Closed** (с остатком → N-r3-2) | Резолв через venv + `pip list`: опубликовано `osv_queries` 0/6/5/4/0, хиты у requests и httpx. Но резолв нарушает `--no-tests` → **B2** |
| N-r2-5 setup.py не читался | **Closed** | AST-парсинг `install_requires`; click: colorama / importlib-metadata → `adapter_gap`, а не «нет зависимостей» |
| N-r2-6 шум S101 | **Not addressed**, в digest не указан как deferred | Опубликованный корпус: S101 = 14/42 (click), 38/44 (flask), 26/48 (httpx), 6/52 (requests), 24/41 (six) |
| N-r2-7 wall_clock / CI / vendor | **Not addressed**; CI явно отложен | `runner.py:189` по-прежнему только пишет `wall_clock_s`; `.github/` нет; vendor/generated не детектируются |

**r1 (повторно проверено):** все пробы r1 зелёные: env-leak `--no-tests` ✓, OSV fail-closed ✓, `re.compile` ✓, worktree ✓, URL userinfo ✓, **determinism: fingerprints / manifest / zip — все `True`** (в r2 manifest на Linux расходился).

---

## Blockers

### B1. Тест PR красный на Linux: `scrub_text_paths` заменяет голую подстроку `/tmp`

```
FAILED tests/test_r2_blockers.py::test_n_r2_3_scrub_linux_tmp_paths
1 failed, 37 passed
```

`scrub_text_paths` добавляет в корни замены `Path(tempfile.gettempdir())`. На Linux это `/tmp`: 4 символа проходят фильтр `len(v) > 3`, и `str.replace("/tmp", "<metria-tmp>")` срабатывает **до** регэкспов. Строка `/tmp/metria-adeq-8v60pk2h/…` превращается в `<metria-tmp>/metria-adeq-8v60pk2h/…`, после чего `_METRIA_TMP` уже не совпадает: случайное имя каталога остаётся. Заодно портится посторонний текст субъекта.

```
$ python3 -c "from pathlib import Path; from metria.sandbox import scrub_text_paths as s; \
  print(s('python /tmp/metria-adeq-8v60pk2h/venv/bin/python', Path('/tmp/subject-root'))); \
  print(s('see /tmpfoo/bar and /tmp/build.log', Path('/x')))"
python <metria-tmp>/metria-adeq-8v60pk2h/venv/bin/python
see <metria-tmp>foo/bar and <metria-tmp>/build.log
```

На macOS `gettempdir()` = `/var/folders/…/T`, поэтому там тест зелёный. Ровно такую регрессию поймал бы CI на `ubuntu-latest` (см. N-r3-6). При повторном прогоне корпуса на Linux в evidence остаются `/tmp/metria-corpus-*` и `/tmp/metria-adeq-*`.

**Закрыть:** не подставлять общий temp-корень как голую подстроку. Заменять только точные известные пути (`root`, `work`, `home`, `evidence_dir`) по границе компонента (`(?<![\w/])<path>(?=/|\b)`), а temp-каталоги Metria — регэкспом по префиксу `metria-*`, независимо от того, как начинается temp-корень. Тест PR должен стать зелёным на Linux; добавить кейс `/tmpfoo` → без изменений.

### B2. `--no-tests` снова исполняет код субъекта: резолв версий делает `pip install -e .` (регрессия r1 B1.2)

Контракт из r1 и README: «`--no-tests` … that mode does **not** import or run subject code». Новый `supply_chain.resolve_runtime_versions()` (N-r2-4) создаёт venv и выполняет `pip install -q -e .` для **любого** субъекта с незапиненными runtime-зависимостями. Это сборка субъекта: `setup.py` / build backend исполняются. Вызов зависит только от `query_osv`, а не от `run_tests`.

**Repro:** `python3 docs/reviews/claude-r3-repro/probe_no_tests_runs_subject_build.py` (шпион на `run_scrubbed`, `run_tests=False`):
```
run_tests=False; subject editable installs issued by supply_chain:
    pip install -q -e .
```
В моей среде pip офлайн, поэтому побочный эффект `setup.py` до исполнения не дошёл (build isolation не скачал setuptools). На машине с сетью, где собран опубликованный корпус, `setup.py` исполнится.

**Закрыть:** резолв через установку разрешён только тогда, когда исполнение кода субъекта разрешено (`run_tests=True` или отдельный явный флаг). Иначе незапиненные зависимости → `adapter_gap` («version unresolved: subject code execution disabled»). Лучше переиспользовать venv из test_adequacy (там всё уже поставлено) вместо второй установки (N-r3-4). Тест: `run_subject(..., run_tests=False)` не выдаёт ни одного `pip install` с `-e`/путём субъекта.

### B3. Падение теста из-за отсутствующего **не**-субъектного модуля на раннере → `fail` (кейс six/`_dbm` из r1 B3)

В `_junit_failure_kinds` любой `<failure>` сразу помечается как дефект субъекта (`if tag == "failure" or …: has_subject = True; continue`), до проверки ImportError. pytest пишет исключения из тела теста именно как `<failure>`, поэтому `ModuleNotFoundError` внутри теста никогда не доходит до атрибуции. Плюс `has_subject = bool(_FAILED_LINE.search(blob) …)`: любая строка `FAILED …` в выводе = субъект. Это ровно строка таблицы r1 B3 (six: «у CPython раннера нет `_dbm` → 1 FAILED → subject ✗»), и она снова даёт неверную атрибуцию.

**Repro:** `python3 docs/reviews/claude-r3-repro/probe_third_party_import_in_test_body.py` (venv-стаб как в пробах r2): `test_ok` проходит, `test_dbm_backend` падает только на `import _dbm_not_on_this_runner`:
```
only failure = missing non-subject module -> status: fail | null_reason: None | classify: fail
```

**Закрыть:** атрибуция по каждому testcase, без глобальных флагов по тексту. Для `<failure>`/`<error>` с `ModuleNotFoundError`/`ImportError` резолвить имя модуля: не модуль субъекта → env, модуль субъекта → subject. `FAILED`-строку из вывода как признак субъекта не использовать (junit уже есть). Итог: хотя бы один subject-кейс → `fail` (env-кейсы отдельной находкой); **только** env-кейсы → `NOT MEASURED / test_env_unavailable`. Тест: проба выше → `NOT MEASURED`; кейсы r2 (A, B) остаются `fail`.

---

## Nits (code-fixable; по playbook закрыть до r4)

- **N-r3-1. `PYTHONUSERBASE` оператора передаётся субъекту, `PYTHONNOUSERSITE=1` убран.** `scrub_env` пишет в env субъекта путь user-base оператора (у меня `/root/.local`; на macOS — `/Users/<имя>/Library/Python/3.9`, то есть имя пользователя). В настоящем venv это ни на что не влияет: проверено, `site.ENABLE_USER_SITE = False` даже с `PYTHONUSERBASE`. Значит, изменение нужно только чтобы стаб `--system-site-packages` в пробах находил pytest на Mac, а платит за это продукт. Вернуть `PYTHONNOUSERSITE=1`, убрать `PYTHONUSERBASE`. Если стабу нужен pytest — ставить его в стаб-venv, а не открывать user-site в продукте.
- **N-r3-2. `==1.*` парсится как пин `"1."`.** `_dep_name_spec` отрезает `*`, `_pinned_version` принимает результат. В опубликованном httpx: `resolved_versions.json` → `"httpcore": "1."`, и OSV опрошен для несуществующей версии — тихий false negative. Repro: `python3 docs/reviews/claude-r3-repro/probe_wildcard_pin.py` → `httpcore==1.* -> spec='==1.' pinned='1.'`. Wildcard/`!=`/составные спецификаторы → не пин; версию брать из `pip list`.
- **N-r3-3. Вердикты корпуса зависят от даты резолва, и это нигде не сказано.** Все три новых FAIL опубликованного корпуса получены на «последних совместимых сегодня» зависимостях, а не на состоянии субъекта на его SHA:
  - **flask 3.0.3** `test_adequacy: fail`: `test_bad_environ_raises_bad_request` (`assert 404…`) при `werkzeug==3.1.9` — версия, которой не было на момент релиза.
  - **requests** `fail`: `TestTimeout::test_connect_timeout` и т.п. — это сетевые тесты, их результат зависит от сети раннера.
  - **httpx** `supply_chain: fail`: hard gate на `GHSA-82r6-8w77-94w6 in anyio==4.12.1`, версия резолвнута сегодня.

  Утверждения не ложные («если поставить сегодня — так»), но в отчёте должно быть видно, на чём они основаны. Нужно: `pip freeze` тестового venv в evidence `test_adequacy` (сейчас резолв пишется только в `supply_chain/resolved_versions.json`), дата резолва в `report.txt`, пометка «subject does not pin deps; result reflects dependency state at <date>», доступность сети раннера в `runner_environment.json`. Сетевые тесты — либо прогон без сети с классификацией connect-ошибок как env, либо явная находка.
- **N-r3-4.** `resolve_runtime_versions` строит **второй** venv и заново ставит субъект, хотя test_adequacy уже поставил всё в свой venv. Время корпуса удваивается, а `wall_clock_s` по-прежнему не соблюдается.
- **N-r3-5 (= N-r2-6, не тронут).** S101 «Use of `assert`» в тестах: 14–38 находок на субъект, у flask это 38 из 44 structural-находок. Исключить S101 на тестовых путях (классификатор по сегментам теперь есть).
- **N-r3-6 (= N-r2-7).** CI отложен с объяснением «у Mac `gh` нет `workflow` scope». Это ограничение токена, а не проекта: workflow можно добавить через веб-интерфейс GitHub или токеном с `workflow` scope. B1 этого раунда — ровно то, что поймал бы job на `ubuntu-latest`. `wall_clock_s` и детекция vendor/generated тоже не закрыты и не перечислены в deferred.

---

## What I did not check

- **macOS / CPython 3.9.6**, профиль опубликованного корпуса. У меня Linux / 3.13.18: красный тест B1 и Linux-утечки путей видны только здесь, а mac-зелёность я не воспроизводил.
- **Фактическое исполнение `setup.py` под `--no-tests`** (B2): pip офлайн, доказательство через argv-шпион, а не через побочный эффект.
- **Живой OSV и реальность хитов:** `api.osv.dev` заблокирован. GHSA по `anyio==4.12.1` (critical → hard gate httpx) и 10 хитов по `urllib3==2.6.3` у requests я не перепроверял. Это вендорские данные OSV на дату прогона.
- **Успешная установка зависимостей субъекта** — только через стаб-venv и опубликованные `install.json`.
- **Windows.**
- **`metri/docs/claude-r2-fix-digest.md`, `metri/docs/claude-r3-ping.md`** — в репозитории их нет (вне ветки PR), не читал. Сверял по коду и пингу.

## Probes / repro

Из корня репозитория.

r1 + r2 (перепрогнаны на этом tip, все ожидаемые результаты):
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
```

r3 (новые):
```bash
python3 -m pytest -q tests/test_r2_blockers.py::test_n_r2_3_scrub_linux_tmp_paths  # B1 → должен быть зелёным на Linux
python3 docs/reviews/claude-r3-repro/probe_no_tests_runs_subject_build.py          # B2 → ни одного `pip install -e .` при run_tests=False
python3 docs/reviews/claude-r3-repro/probe_third_party_import_in_test_body.py      # B3 → NOT MEASURED / test_env_unavailable
python3 docs/reviews/claude-r3-repro/probe_wildcard_pin.py                         # N-r3-2 → pinned=None для ==1.*
python3 scripts/run_corpus.py --out "$PWD/.rerun"   # на Linux: grep -r '/tmp/metria-' .rerun → пусто
```
