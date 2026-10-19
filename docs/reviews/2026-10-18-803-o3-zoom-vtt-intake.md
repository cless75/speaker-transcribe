---
requested_by: codex
reviewer: codex-astra
reviewer_model: openai/gpt-6-astra
author: codex
author_model: m4/gpt-6-sol
owner_project: 803
subject: "803-o3: opt-in Zoom VTT-only intake, SQLite dispatcher, private config and systemd templates; speaker-transcribe ebd74f3..d2c8074"
roles: [adversary, operator, integrator, gate-keeper]
status: requested
opened: 2026-10-18
closed:
unit: 803-o3
unit_form: dor-open
outcome:
return_reason:
---

# Review: 803-o3 — Zoom VTT-only intake in 801

## Запрос

**Предмет:** `D:\work\speaker-transcribe`, коммиты `dd01f81`, `42eb4d2`,
`ca6d569`, `d2c8074` относительно `ebd74f3` (коммит review-запроса `1fa9b13`
не является частью проверяемого кода). Раздатчик читает локальный SQLite zoomrs только
на чтение, доставляет VTT+чат в `Hub/{pid}/_{pid}_inbox/{wN}/`; вотчер получает
opt-in VTT-only вход. MP4 остаётся на локальном диске. Скрипт приватного YAML
держит `sync_job`, `download_job` и три флага удаления выключенными.

**Сначала G0/G2:** сверить решения
`org/zoom-recording-delivery-stays-in-our-contour`,
`org/zoomrs-repository-local-and-backlog-batched-to-nas`,
`org/zoom-vtt-only-input-triggers-watcher-without-hub-video`,
`org/zoomrs-local-recordings-never-auto-evicted` и карточку
`D:\work\cless75-ex1\areas\60-techplatform\803-learning-media-pipeline\803-o3-Auto-Capture-Fresh-Zoom-Recordings.md`.
При запуске Astra на Dev-сервере отображать `D:\work\speaker-transcribe` на
`/home/cless/Projects/reviews/803-o3-speaker-transcribe`, а `D:\work\cless75-ex1`
на `/home/cless/Projects/cless75-ex1`; не искать Windows-диски на Linux.
Если задача противоречит текущему Drive-first пакету другой сессии, вернуть
противоречие **до** написания находок; ничего не интегрировать молча.

**Проходы по ролям:**

- `adversary` — попробовать опровергнуть идемпотентность, атомарность выдачи,
  стабильность записи, отсутствие записи в несмонтированный Hub, защиту от чужих
  путей в SQLite и ложного `asr-done` при битом VTT.
- `operator` — запустить `python -m pytest -q` в репо и offline-прогон
  `tests/test_zoom_dispatch.py`/`tests/test_zoom_vtt_import.py` на временном каталоге;
  не пользоваться настоящими Zoom-кредами, Hub участников и production-таймерами.
  Если на Dev-сервере нет тестовых Python-зависимостей, отмечать прогон
  «не проверено», не скачивать тяжёлый ASR/GPU-стек ради одного review.
- `integrator` — проверить контракт `audio_inbox_watch.py`/VTT sidecar/состояние,
  отображение пути контейнера `/data/recordings` в host `repository`, совместимость
  с будущим `hub_intake.py` без запуска двух consumers.
- `gate-keeper` — проверить, что пример и юниты не запускают загрузку/удаление до
  оценки бэклога, не выводят секреты, не включают GPL-код в MIT-репо. Отдельный
  локальный GPL-клон на Lenovo — `~/zoomrs`, ветка `zoom-vtt-local-intake`, коммит
  `08d5ba4`, образ `zoomrs:vtt-20261018`; **не** часть публичных коммитов 801.

**Границы:** `README.md`, `src/hub_intake.py`, `tests/test_hub_intake.py` и шаблоны
`hub-intake-status@.*` — незакоммиченная работа другой сессии, не править и не
включать в мой пакет. Никакого `git push`, `git pull` на Lenovo, включения systemd,
доступа к Zoom API или тестового удаления данных до review-вердиктов.
Read-only сверка одной текущей облачной встречи показала `active_speaker` MP4 и
`audio_transcript` VTT, без `chat_file`. Поэтому в `d2c8074` добавлен fallback
`active_speaker`, а отсутствие чата должно обрабатываться без выдумывания содержимого.

Критерий находки: уровень по контракту, адрес `файл:строка`, воспроизводимое
последствие и проверка; не делать вывод «готово к production» только по unit tests.

## Находки

<!-- заполняет reviewer; проходы по ролям отдельно -->

## Вердикты

<!-- заполняет author на каждую находку после ответа reviewer -->
