═══════════════════════════════════════════════════════════════
CURSOR ↔ CLAUDE CODE REVIEW VIA GITHUB — PLAYBOOK (v1)
Для другого проекта / другого программиста. Независимо от Zorro2.
═══════════════════════════════════════════════════════════════

ЦЕЛЬ
  Cursor пишет код и открывает PR.
  Claude читает тот же PR на GitHub, гоняет проверки, пишет
  вердикт в репозиторий на ту же ветку.
  Обмен идёт только через git + GitHub. Без zip, без paste-паков кода.

ПОЧЕМУ GITHUB, А НЕ ORIGIN/CURSOR FORGE
  Среда Claude часто не достучится до Origin (proxy 403) и не имеет
  Cursor-аккаунта. GitHub + Claude GitHub App (Contents + Pullrequests
  Read and write) — рабочий канал.

───────────────────────────────────────────────────────────────
0. РОЛИ
───────────────────────────────────────────────────────────────
  OWNER     — человек: аккаунты, доступы, пинги Claude, merge.
  CURSOR    — агент/разработчик: ветка, CI, фиксы, dual-push если нужен SoT.
  CLAUDE    — внешний ревьюер: читает PR, пишет docs/reviews/claude-rN.md.

───────────────────────────────────────────────────────────────
1. ОДНОРАЗОВАЯ НАСТРОЙКА (OWNER)
───────────────────────────────────────────────────────────────
A) Репозиторий на GitHub (private OK).
   Пример: github.com/<org-or-user>/<repo>

B) Cursor (или IDE-агент) должен уметь push в этот repo
   (SSH deploy key / gh auth / PAT с repo + workflow если есть Actions).

C) Связать Claude ↔ GitHub:
   1. В Claude: Connectors / Integrations → GitHub → Link аккаунт.
   2. ВАЖНО: после Link открыть НОВЫЙ чат Claude
      (старая сессия не подхватывает связку).
   3. GitHub → Settings → Applications → Installed GitHub Apps
      → Claude → Configure
      URL: https://github.com/settings/installations
   4. Repository access: All ИЛИ Only select → добавить <repo>
   5. Permissions: Contents = Read and write
                   Pull requests = Read and write
   6. Сохранить. В чате Claude: «готово» / попросить add_repo.

D) Проверка доступа Claude:
   «list_repos / add_repo <org>/<repo>»
   Claude должен видеть private repo. Если видит только public —
   App не получил доступ к private (шаг C).

E) (Опционально) Второй remote «SoT» (Origin/GitLab/др.):
   Cursor SoT = forge A, Claude mirror = GitHub.
   После каждого фикса Cursor: push tip на ОБА remote одной ветки.
   Claude работает только с GitHub.

───────────────────────────────────────────────────────────────
2. КОНВЕНЦИИ В РЕПО (зафиксировать в README или CONTRIBUTING)
───────────────────────────────────────────────────────────────
  • Ветка фичи/среза: одна долгоживущая PR-ветка на цикл ревью
    (или новый PR на major slice — но номер PR стабилен для пинга).
  • Отчёты Claude ТОЛЬКО:
      docs/reviews/claude-rN.md
      docs/reviews/claude-rN-repro/   (опционально probes)
    N = 1,2,3… монотонно. Не править старые rN без пометки errata.
  • Вердикт в шапке отчёта ровно один из:
      Accept | Accept-with-nits | Blockers
  • Blockers: B1, B2… с воспроизведением.
  • Nits: N-1… не блокируют merge сами по себе, но Cursor
    по политике команды закрывает code-fixable nits до следующего
    раунда (не оставлять «optional»).
  • Claude коммитит отчёт НА ТУ ЖЕ ветку PR (не в main, не отдельный PR).
  • Cursor не переписывает отчёты Claude; только код/контракты/CI.

───────────────────────────────────────────────────────────────
3. ЦИКЛ РАБОТЫ (ПОВТОРЯТЬ)
───────────────────────────────────────────────────────────────

[1] CURSOR: сделать срез
    - ветка от main
    - тесты зелёные локально
    - push → открыть PR #N на GitHub
    - (если dual remote) push ту же ветку/SHA на SoT

[2] OWNER → CLAUDE (новый чат после link): короткий пинг
    Шаблон:

    проверь PR <N>

    Repo: https://github.com/<org>/<repo>
    PR:   https://github.com/<org>/<repo>/pull/<N>
    Head tip: <full-sha>   # опционально, но лучше указать
    Branch: <branch-name>

    Прочитай diff/ветку. Прогони тесты/пробы по силам среды.
    Закоммить docs/reviews/claude-r<K>.md на эту же ветку PR.
    Verdict: Accept | Accept-with-nits | Blockers.
    Если Blockers — B1… с шагами воспроизведения.

[3] CLAUDE: ревью + push отчёта на ветку PR
    Tip после push: <sha>. OWNER/CURSOR подтверждают ls-remote.

[4] OWNER → CURSOR: вставить вердикт Claude (или «читай claude-rK.md»)

[5] CURSOR при Blockers / Accept-with-nits:
    - закрыть ВСЕ Blockers
    - закрыть code-fixable nits (gofmt, валидации по контракту,
      флаки тестов, CVE bump если указан, CI -p 1 и т.п.)
    - явно отложить только out-of-scope (нет tooling, product decision,
      чужой forge) — списком в digest
    - dual-push tip на GitHub + SoT
    - выдать OWNER готовый пинг для следующего r(K+1)

[6] OWNER → CLAUDE: пинг r(K+1)

    Claude r<K+1> please re-review PR <N> tip after previous findings close.

    Prior tip you reviewed: <old-sha> (docs/reviews/claude-rK.md).
    This tip: <new-sha> on branch <branch>
    (confirm via git ls-remote / PR head).

    Closed:
    - B-… : <one line each>
    - nits: <one line each>

    Deferred on purpose: <list>

    Write docs/reviews/claude-r<K+1>.md on this branch.
    Verdict: Accept | Accept-with-nits | Blockers.
    Explicitly say whether prior Blockers are closed.

[7] Повторять 4–6 пока Verdict = Accept
    (или Accept-with-nits + письменное «merge as-is» от OWNER).

[8] MERGE
    - merge PR в main на GitHub
    - если dual remote: fast-forward / mirror main на SoT
    - тег/релиз по правилам проекта

───────────────────────────────────────────────────────────────
4. ШАБЛОН ОТЧЁТА CLAUDE (минимум)
───────────────────────────────────────────────────────────────
  # Claude rK — <date>
  Verdict: …
  Tip reviewed: <sha>
  Scope: <что читал / что гонял>

  ## Blockers (если есть)
  ### B1 …
  ### B2 …

  ## Nits
  ### N-1 …

  ## What I did not check
  - …

  ## Probes / repro
  - path to probes or commands

───────────────────────────────────────────────────────────────
5. ШАБЛОН DIGEST CURSOR ПОСЛЕ ФИКСА (для людей + следующий пинг)
───────────────────────────────────────────────────────────────
  Tip: <sha> (both remotes if dual)
  Closed: …
  Deferred: …
  Verify: команды тестов
  Claude r(K+1) ping: (готовый paste-блок)

───────────────────────────────────────────────────────────────
6. ТИПИЧНЫЕ СБОИ И ЧИНИЛКИ
───────────────────────────────────────────────────────────────
  Claude «не видит repo»
    → App Configure не добавил private repo; или старый чат без re-link.

  Claude push 403 / нет Write
    → Contents + Pull requests не Read and write; или нет add_repo.

  Cursor cloud VM не пушит в GitHub
    → нет creds fow830/org; пуш с машины OWNER (gh/SSH) или self-hosted.

  GitHub tip ≠ Origin tip
    → histories diverged (Claude commits only on GH). Merge или
      cherry-pick; dual-push один tip; не оставлять SoT позади.

  PR head «старый» в ls-remote
    → подождать / перепушить ветку; сверить refs/pull/N/head.

  CI красный только в Actions
    → воспроизвести ТОЧНУЮ команду из ci.yml (часто нужен -p 1
      при shared test DB).

  «Accept-with-nits» и сразу merge
    → только если OWNER явно сказал; иначе закрыть nits → r(K+1).

───────────────────────────────────────────────────────────────
7. МИНИМАЛЬНЫЙ ЧЕКЛИСТ ЗАПУСКА В НОВОМ ПРОЕКТЕ
───────────────────────────────────────────────────────────────
  [ ] GitHub repo создан
  [ ] Cursor умеет push
  [ ] Claude GitHub App → repo + R/W Contents & PRs
  [ ] Новый чат Claude после Link
  [ ] Папка docs/reviews/ заведена (можно пустым .gitkeep)
  [ ] Договорённость: Accept / Accept-with-nits / Blockers
  [ ] Первый PR открыт, tip SHA известен
  [ ] Первый пинг «проверь PR N» отправлен
  [ ] (если SoT≠GitHub) скрипт/привычка dual-push

───────────────────────────────────────────────────────────────
8. ЧЕГО НЕ ДЕЛАТЬ
───────────────────────────────────────────────────────────────
  • Не слать Claude zip с исходниками, если GitHub уже доступен.
  • Не просить пароль GitHub в чат; только App / SSH / gh на машине OWNER.
  • Не ревьюить в Origin, если Claude туда не ходит.
  • Не менять номер PR каждый мелкий фикс — ломает «проверь PR N».
  • Не считать «готово» после Blockers без push фикса + нового rN.
  • Не оставлять «optional nit» без digest deferred-списка.

═══════════════════════════════════════════════════════════════
Конец playbook. Копируй целиком в README другого проекта.
═══════════════════════════════════════════════════════════════
