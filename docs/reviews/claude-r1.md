# Claude r1 — 2026-11-06

**Verdict: Blockers**
**Tip reviewed:** `bac6f159f32ea334436bbf7e1e94bae74a772aa7` (`cursor/mvp-cli-corpus-dafb`, PR #2)
**Scope:** весь diff PR #2 против `main` (`78cfbde`) прочитан полностью: `metria/**`, `schema/**`, `policy/`, `scripts/run_corpus.py`, `tests/**`, `corpus/subjects.yaml`, все 5 опубликованных `corpus/results/*` (`stress-run.json`, `report.txt`, evidence), README, pyproject, playbook.
**Прогнано:** `pytest -q` → **19 passed**; 6 проб в `docs/reviews/claude-r1-repro/`; **полный повторный прогон корпуса** (`scripts/run_corpus.py`) в моей среде (Linux, CPython 3.13.18, без `ruff` как модуля, OSV недоступен через egress) и дифф с опубликованными результатами.

---

## Что хорошо (держать)

- Схема v0.1 точно соответствует контракту из ревью: `score: null` + `score_reason: "uncalibrated"`, `additionalProperties: false`, без `band`, `null_owner` → `UNDECLARED / NOT MEASURED` через `if/then`, Protectability разделён на `supply_chain` + `controls`, `normative` принудительно `experimental`. Негативные фикстуры и `test_forbidden_aggregate_fields` — правильная защита.
- Агрегация только через hard gates, null не тянет статус.
- `httpx` → `NOT MEASURED / build_failed / null_owner=metria`: ровно та атрибуция, которой я добивался.
- Корпус опубликован вместе с FAIL. Это правильный жанр.

Схема хорошая. Блокеры ниже касаются того, что адаптеры **пишут в эту схему**: в нескольких местах продукт публикует ложные утверждения о сторонних проектах или выдаёт сбой собственного инструмента за PASS.

---

## Blockers

### B1. Код субъекта исполняется на хосте со всем окружением оператора, включая `--no-tests`

`test_adequacy` запускает `python -m pytest` (сначала `--collect-only`, потом run) в интерпретаторе самого Metria с `env={**os.environ, ...}`. Collection импортирует `conftest.py` и тестовые модули субъекта, то есть исполняет произвольный код ещё до прогона. `--no-tests` («Discover/collect only; do not execute pytest») этот код **всё равно исполняет**. Песочницы нет: ни контейнера, ни отдельного venv, ни очистки env. Для профиля `cold_external` (чужой код по определению) это вредоносный вектор, а не нюанс.

**Repro:** `docs/reviews/claude-r1-repro/probe_env_leak.sh`. Субъект с `tests/conftest.py`, который пишет имена переменных окружения в файл, запускается с `--no-tests`. В моей среде код субъекта увидел `GH_TOKEN`, `GITHUB_TOKEN`, `GIT_CONFIG_KEY_*` (печатаются только имена, значения доступны так же).

**Закрыть (минимум для r2):**
1. Env allowlist для любого subprocess субъекта (`PATH`, `HOME`=tmp, `LANG`, `PYTHON*` по необходимости; всё остальное, включая `*TOKEN*`, `*KEY*`, `*SECRET*`, `AWS_*`, `GH_*`, `GIT_*`, вырезать). Тест: проба выше должна дать пустой список.
2. `--no-tests` = **ноль исполнения кода субъекта** (без collect). Учёт тестов в этом режиме — только статический (`layout.json`).
3. Исполнение в изолированном окружении (отдельный venv в tmp как минимум; контейнер — до любого hosted/Action). В README явное предупреждение: «`metria run` исполняет код субъекта».

### B2. Сбой OSV выдаётся за `supply_chain: PASS` (fail-open)

`_osv_query` ловит `URLError/TimeoutError/JSON` и возвращает `[]`, а это неотличимо от «уязвимостей нет». Счётчик `osv_errors` увеличивается только во внешнем `except`, до которого исключение не доходит, поэтому он всегда 0.

**Repro 1:** `python3 docs/reviews/claude-r1-repro/probe_osv_fail_open.py` → `status: pass | osv_errors: 0 | osv_queries: 2` при принудительно упавшей сети и заведомо уязвимых `jinja2==3.1.2`, `urllib3==1.26.0`.
**Repro 2, реальный корпус:** в моей среде `api.osv.dev` недоступен. Повторный прогон: **httpx `supply_chain` WARN → PASS, flask WARN → PASS** (14 и 20 запросов упали молча, `osv_errors=0`).

**Закрыть:** любой неуспешный запрос → `osv_errors += 1`; если `osv_errors > 0` → линза `null / tool_error / null_owner=metria` (или как минимум `warn` + `confidence: low` + явная находка «CVE lookup incomplete: N/M»). Никогда `pass`. Тест: проба выше даёт не-`pass`.

### B3. Test Adequacy измеряет окружение раннера, а одна и та же причина атрибутируется то субъекту, то Metria

Зависимости субъекта не ставятся; pytest запускается в том интерпретаторе, где установлен Metria. Поэтому результат определяется тем, что случайно лежит в site-packages раннера:

| субъект | причина | опубликовано / у меня | атрибуция |
|---|---|---|---|
| httpx | нет `trustme` (test dep) | NOT MEASURED / NOT MEASURED | metria ✓ |
| requests | нет `pytest-httpbin` → `fixture 'httpbin'` ERROR ×20 | **FAIL** / FAIL | **subject ✗** |
| six | у CPython раннера нет `_dbm` → 1 FAILED | WARN / **FAIL** | **subject ✗** |
| click, flask | у меня нет test deps → collect fail | WARN / **NOT MEASURED** | — |

`requests v2.32.3` опубликован как «test suite failed». Это ложное утверждение о стороннем проекте: падают не тесты requests, а у раннера нет их test-зависимостей.

**Закрыть:**
1. Разделить exit-причины pytest: ошибки setup/collection/import/fixture (`ERROR`, exit 2/3/4, `ModuleNotFoundError`, `fixture ... not found`) → `null / test_env_unavailable / metria`; `fail` только на assertion-провалах (`FAILED` без env-ошибок).
2. Ставить test-deps субъекта в изолированное окружение из B1.3 (`pip install -e .[test]` / `requirements*test*`/`dev` по discovery) с записью того, что поставлено. До этого любой не-зелёный прогон с import/fixture-ошибками считается NOT MEASURED.
3. В `tools`/`raw_metrics` писать версию Python и платформу раннера.

### B4. Корпус невоспроизводим и не указывает, на чём прогнан

Корпус — главный acceptance-артефакт этого PR. Повторный прогон того же кода на тех же SHA дал **5 расхождений статусов линз на 5 субъектах** (B2 + B3) и смену `overall` у six (warn → fail). Причины не только в сети:
- `ruff` запускается как `sys.executable -m ruff`. Установленный CLI-ruff (у меня `/root/.local/bin/ruff`) не находится, линза молча теряет детектор (`ruff_ran=False`), а six в опубликованной версии имеет 16 ruff-находок. Отсутствие инструмента нигде не отражено как снижение покрытия.
- `runner_image_digest: null`, версии Python и ОС нигде нет. По путям видно, что опубликованный корпус собран на macOS (`/private/var/folders/...`), а пути протекают в `subject_manifest.root`, `collect.txt`, `pytest.txt`.

**Закрыть:** записывать окружение раннера (OS, Python, версии каждого инструмента, включая `absent`); отсутствующий обязательный детектор → понижение `confidence` + находка/пометка, а не тишина; убрать абсолютные tmp-пути из evidence (относительно корня субъекта); в README честно: «корпус воспроизводим на образе X», и дать команду/образ. Повторный прогон в CI или на втором раннере должен совпадать по статусам и fingerprints.

### B5. Supply chain публикует чужие уязвимости как уязвимости субъекта

`inventory_packages` сливает в одну «поставку субъекта» все `requirements*.txt` из дерева, включая примеры, docs и dev. Runtime-зависимости из `pyproject.toml`/`setup.py` не парсятся (файл попадает в `lockfiles`, но не читается).
- **flask 3.0.3:** 28 OSV-хитов, 12 «high» из `examples/celery/requirements.txt`, в том числе **«GHSA-68rp-wp8r-4726 in flask==2.3.2»**. Отчёт по Flask 3.0.3 утверждает, что у Flask уязвимый Flask 2.3.2.
- **httpx:** 15 хитов из `requirements.txt`, а это dev-tooling httpx, не runtime.
- **requests:** runtime-deps (urllib3, idna, charset-normalizer, certifi) не прочитаны, 5 unpinned пропущены, 2 запроса → **PASS**.
- Severity: `"9." in score` / `score[:1] in {"7","8"}` никогда не срабатывают на CVSS-векторах OSV (`CVSS:3.1/AV:N/...`), а по умолчанию ставится `high`. Итог: все хиты «high», `critical` недостижим, и hard gate `critical_is_hard_gate: true` из политики фактически мёртвый код.

**Закрыть:** классифицировать источник (`runtime | dev | docs | example | test`) по пути/имени, в CVE-вывод линзы брать только `runtime` (остальное — в `raw_metrics` отдельно); парсить `[project.dependencies]` (`tomllib` в 3.11+, иначе явный `adapter_gap`); severity брать из `database_specific.severity` / посчитанного CVSS base score, иначе `severity` не ставить (или `unknown`), не `high`. Для unpinned — `null`/`adapter_gap` по пакету, не «нет данных → pass». Тест: фикстура с `examples/x/requirements.txt` не влияет на статус.

### B6. Hard gate на неподтверждённой regex-находке в тестовых фикстурах

`requests` FAIL с hard gate из-за `-----BEGIN PRIVATE KEY-----` в `tests/certs/**`. Это TLS-фикстуры тестов, а не утёкшие секреты. В политике сказано «hard-fail only on high-confidence HEAD-tree key material», в ревью (Q8.6) — «hard gate только на `confirmation ≥ multi_tool` или детерминированно валидированных классах». Здесь `confirmation: unconfirmed`, детектор один (regex), а путь явно тестовый. Отчёт поднимает это в TOP RISKS пять раз (#1 и #2 — один и тот же файл, см. N-7).

**Repro:** `corpus/results/requests@61e2240f283f/report.txt`.

**Закрыть:** ключи под `tests/`, `testdata/`, `fixtures/`, `certs/` внутри тестов → находка `warn` с `lens_routing_rule` и пометкой `test_fixture`, без hard gate; hard gate только для unconfirmed regex вне тестовых путей + (желательно) проверки формы ключа. Тест на фикстуре.

---

## Nits (code-fixable; по playbook закрыть до r2)

- **N-1. `re.compile()` считается «Use of compile() on dynamic input».** `_DangerVisitor` сравнивает только `attr`, поэтому срабатывают `re.compile`, `obj.exec`, `x.eval`. Это одна из основных причин, почему structural = WARN у всех субъектов (httpx: 17 из 25 находок, requests: 8). Repro: `probe_recompile.py` → `status: warn`. Матчить только builtins (`ast.Name`) или полностью квалифицированные вызовы.
- **N-2. Ruff:** `data[:40]` режется **до** фильтра интересных кодов, и S/B-находки за 40-й позицией теряются. Ruff исполняет конфиг субъекта (`[tool.ruff]`, `# noqa`) без записи в `suppressions_applied` — это gaming pattern №1. Запуск через `-m ruff` см. B4. Нужно `--isolated` (или явный ruleset) и учёт noqa.
- **N-3. `metria run <local> --sha X` делает `git checkout X` в рабочей копии оператора** (repro: `probe_local_sha_mutates.sh`, ветка → detached HEAD; на грязном дереве упадёт или потеряет контекст). Нужен `git worktree add` во временный каталог.
- **N-4. `_parse_target`** считает `@` из userinfo разделителем SHA: `https://oauth2@gitlab.example.com/group/repo` → `('https://oauth2', 'gitlab.example.com/group/repo')` (repro: `probe_parse_target.py`). Парсить через `urllib.parse`, SHA проверять `^[0-9a-f]{7,40}$`.
- **N-5. `run_corpus.py --out` вне репозитория падает** на `out_dir.relative_to(ROOT)` (ValueError) после первого субъекта, и `SUMMARY.json` не пишется.
- **N-6. `evidence.zip` недетерминирован** (mtime в zip-заголовках; repro `probe_determinism.sh`: fingerprints и manifest совпадают, sha zip — нет). Фиксировать `date_time`/порядок. Корпус коммитит и zip, и распакованное дерево — выбрать одно (zip либо дерево + manifest).
- **N-7. `top_risks`:** hard gate получает псевдо-fingerprint `hardgate:<lens>`, которого нет среди findings (висячая ссылка вопреки имени поля), и из-за дедупа по fp соседствует с самой находкой (requests: #1 = #2). Ссылаться на fingerprint исходной находки.
- **N-8.** `budget.wall_clock_s` не соблюдается (только записывается); `--profile` молча игнорируется; в `runner._architectural_undeclared` мёртвый `if ...: pass` и комментарий «Claude: …» — убрать.
- **N-9. `analysis_scope`** одинаковый для всех линз (Python-LOC даже у `supply_chain`/`controls`); `excluded_paths` = статический `SKIP_DIRS`, даже если таких каталогов нет; vendor/generated не детектируются; тесты/примеры входят в structural scope без пометки. Поле, которое я назвал главным, пока не несёт информации.
- **N-10.** Нет CI (`.github/workflows`), PR проверяется только «19 passed locally». Валидатор создаётся без `FormatChecker`, `date-time` не проверяется.
- **N-11.** `tree_digest` = sha256 от строки tree-id. Либо назвать честно (`git_tree:<id>`), либо считать по содержимому. Для локального пути с грязным деревом digest врёт (берётся `HEAD^{tree}`).
- **N-12.** six и click получают `supply_chain: WARN «No lockfile»`: у библиотеки без runtime-зависимостей это не находка. `setup.cfg`/`setup.py` `install_requires` не читаются. Нет зависимостей и нет файлов → `pass` с `components=0` и пояснением, либо `UNDECLARED` — но не WARN по умолчанию.

---

## Prior blockers

r1 — первый раунд, prior blockers нет.

## What I did not check

- **macOS-раннер**, на котором собран опубликованный корпус. Расхождения с ним показал повторный прогон на Linux; исходную среду сравнить не могу.
- **Живые ответы OSV:** `api.osv.dev` заблокирован egress-прокси моей сессии. Опубликованные OSV-хиты я прочитал и проверил на атрибуцию (B5), но не перезапросил. Именно эта недоступность и вскрыла B2.
- **Python 3.9–3.12** (`requires-python = ">=3.9"`): доступен только 3.13.18. В частности не проверен разбор `pyproject` через `tomllib` (3.11+), если его добавят по B5.
- **Windows** и пути с `\`.
- **GitHub Actions** проверить нечем: workflow в репозитории нет (N-10).
- Playbook (`docs/CURSOR-CLAUDE-GITHUB-REVIEW-PLAYBOOK.md`) прочитан как процессный документ, на код не влияет.

## Probes / repro

Все в `docs/reviews/claude-r1-repro/`, запускаются из корня репозитория:

```bash
bash   docs/reviews/claude-r1-repro/probe_env_leak.sh          # B1
python3 docs/reviews/claude-r1-repro/probe_osv_fail_open.py    # B2
python3 docs/reviews/claude-r1-repro/probe_recompile.py        # N-1
bash   docs/reviews/claude-r1-repro/probe_local_sha_mutates.sh # N-3
python3 docs/reviews/claude-r1-repro/probe_parse_target.py     # N-4
bash   docs/reviews/claude-r1-repro/probe_determinism.sh       # N-6
python3 scripts/run_corpus.py --out "$PWD/.rerun"              # B3/B4: diff with corpus/results/SUMMARY.json
```

Ожидаемо после фиксов: env-leak пустой; OSV-проба не `pass`; `re.compile` без находки; рабочая копия не перемещена; userinfo-URL корректно разобран; zip sha совпадает между прогонами; повторный корпус совпадает с опубликованным по статусам (при недоступном OSV — `NOT MEASURED`, а не `PASS`).
