---
requested_by: codex
reviewer: codex-astra
reviewer_model: openai/gpt-6-astra
author: codex
author_model: m4/gpt-6-sol
owner_project: 801
subject: "801: Drive-first Hub intake worker, verified publication and status pages; speaker-transcribe f0e04c2..cc9cfff"
roles: [adversary, operator, integrator, gate-keeper]
status: requested
opened: 2026-10-20
closed:
unit: 801-drive-first-intake
unit_form: dor-open
outcome:
return_reason:
---

# Review: Drive-first Hub intake worker and status pages (`cc9cfff`)

## Запрос

**Предмет:** `D:\work\speaker-transcribe`, коммит `cc9cfff` относительно
`f0e04c2`. Пакет добавляет `src/hub_intake.py` (прямой Drive-инвентарь,
локальная доставка, HTML, отчёт гигиены), `src/hub_worker.py` (явный E2E-воркер:
локальный стейдж → ASR или VTT-only → сверка публикации → `asr-done` последним),
их тесты и шаблоны `scripts/systemd/hub-intake-status@.*`/`hub-worker@.*`.
Все шаблоны **не установлены**; массовый запуск заблокирован гейтом медиа-E2E.

**Доставка:** `git bundle` с **исходными** объектами, prerequisite `f0e04c2`
(коммит review-запроса 803-o3 `1fa9b13` и Zoom-коммиты `dd01f81`..`f0e04c2`
в предмет **не** входят). Bundle: `/home/cless/Projects/reviews/801-drive-first/801-drive-first-cc9cfff.bundle`
(реф `refs/tags/801-drive-first-review` → `cc9cfff`). Оригинальный SHA сохранён.

**Сначала G0/G2:** сверить решения
`org/hub-intake-local-stage-all-projects`,
`org/hub-project-status-html-in-drive`,
`org/hub-hygiene-report-only-no-auto-delete`,
`org/hub-local-media-retention-after-publication-30-days`,
`org/hub-historical-standalone-vtt-import-at-cutover`,
`org/zoom-vtt-only-input-triggers-watcher-without-hub-video`,
`org/zoom-vtt-ambiguous-note-fails-closed` и карточку
`areas/60-techplatform/801-*/801-*`. Границы и вердикты 803-o3 закрыты отдельно
(см. `docs/reviews/2026-10-18-803-o3-zoom-vtt-intake.md`, outcome `returned`);
Drive-first пакет **не** входит в тот review. Если задача противоречит решениям,
вернуть противоречие **до** написания находок.

**Проходы по ролям:**

- `adversary` — опровергнуть: (1) двойную обработку одного источника двумя
  потребителями при активном старом вотчере; (2) ложный `asr-done` при битом или
  частично доставленном медиа; (3) подмену ревизии между стейджем и публикацией;
  (4) утечку локального пути/речи в Drive или systemd-journal; (5) удаление
  чего-либо в Hub. Особое внимание: `source_unchanged`, `remote_verified`,
  `marker_unique_id`, `run_claimed` (общий claim + heartbeat), `single_worker`.
- `operator` — прогнать `python -m pytest -q` в репо; отдельно offline-прогон
  `tests/test_hub_intake.py` и `tests/test_hub_worker.py` на временном каталоге.
  Не использовать настоящие Zoom-креды, Hub участников и production-таймеры.
  Если на Dev нет тестовых зависимостей, отметить «не проверено», тяжёлый
  ASR/GPU-стек не разворачивать.
- `integrator` — контракт с `src/audio_inbox_watch.py` (маркеры, статусы,
  `claim`, маршрутизация `_inbox`), формат `pipeline/state.json`, совместимость
  HTML статуса с уже опубликованными страницами, отсутствие двух consumers.
- `gate-keeper` — проверить, что юниты по умолчанию не запускают ASR/удаление,
  не выводят секреты, что `hub-worker@.service` требует свежий медиа-E2E и
  неактивный старый таймер, что локальная очистка удаляет **только** локальные
  копии после проверенной публикации.

**Границы:** не выполнять `git push`/`git pull` на Lenovo, не включать systemd,
не обращаться к Zoom API, не удалять данные Hub. Живой production E2E и
синхронизация Drive-first **не подтверждены** этим запросом.

Критерий находки: уровень по контракту, адрес `файл:строка`, воспроизводимое
последствие и проверка; вывод «готово к production» по одним unit tests не делать.
