# ZoomL / zoomrs — аудит забора записей для 801

Версия: 0.1.0 · Проверено: 2026-10-26 · Статус: анализ кода и конфигурации

Под интеграцией ZoomL в этом анализе исследована установленная на Lenovo
`zoomrs`: отдельный GPL-клон `/home/clessd/zoomrs`, HEAD `d764479`, образ
`zoomrs:vtt-20261019`, сервис `zoomrs@clessd`. Другой компонент с буквальным
именем ZoomL не идентифицирован. Проверка read-only, без вызова sync/download,
изменения конфигурации, сборки образа или обращения к Zoom с credentials.

Связанный контракт: [Zoom recording bundles](zoom-recording-bundle-spec.md).

## 1. Фактический путь данных

```text
Zoom /users/me/recordings (один день, pagination)
  -> Go Meeting.Records = recording_files
  -> SyncMeetings: фильтр типов, только ранее неизвестные UUID
  -> SQLite meetings + records (queued)
  -> DownloadJob / CLI DownloadOnceOf
  -> grab.Get в repository/<дата>/<record ID>/<имя из HTTP-загрузки>
  -> проверка HTTP/размера/расширения -> downloaded + path
  -> 801: join SQLite по meeting UUID -> MP4 + VTT
```

Это НЕ готовый каталог одной встречи с READY. MP4 и VTT одной встречи лежат
в разных каталогах record ID. Связь восстанавливается из SQLite, не соседством
файлов. При переносе только файлов без индекса теряется надёжное сопоставление.

## 2. Фактическая конфигурация и содержимое

Безопасная выборка приватной конфигурации (секреты не включены):

```yaml
server:
  sync_job: false
  download_job: false
syncable:
  important: [shared_screen_with_gallery_view]
  alternative: [shared_screen_with_speaker_view, active_speaker]
  optional: [audio_transcript, chat_file]
  min_duration: 0
storage:
  repository: /data/recordings
  keep_free_space: 0
client:
  trash_downloaded: false
  delete_downloaded: false
  delete_skipped: false
```

Контейнер running с 2026-10-20; каталог конфигурации bind-mounted read-only.
Автоматические sync/download в текущем файле выключены. Перезапускать или
включать их в рамках анализа нельзя. Это не мешает ASR работать с уже скачанным.

SQLite на момент проверки: **2 встречи, 4 записи**, все downloaded:
2 `active_speaker` MP4 и 2 `audio_transcript` VTT. Общих M4A, participant audio
и chat records в этой локальной БД нет. Это не утверждение об их наличии в Zoom:
полный актуальный cloud inventory этим анализом не запрашивался.

Монтирования:

- `/home/clessd/zoom-recordings` -> `/data/recordings`;
- `/home/clessd/.local/share/zoomrs` -> `/data/state`;
- `/home/clessd/.config/zoomrs/config.yml` -> `/app/config.yml` (read-only).

## 3. Что скачивается и что отбрасывается

`repo/repo.go:SyncMeetings` собирает три группы:

- Если есть хотя бы один important record, берёт **все important**, alternative
  отбрасывает целиком.
- Иначе берёт **все alternative**, не первый по порядку списка конфигурации.
- Всегда добавляет optional, если они есть.

Следовательно, список alternative — не приоритетный выбор одного видео. Если
Zoom вернул speaker-view и active-speaker без gallery, оба могут попасть в БД.
Текущий downstream-worker выбирает тип отдельно и требует единственный файл
выбранного типа. При нескольких сегментах одного типа он не выбирает их все.

**Общий `audio_only`:** константа существует в Go model, но тип отсутствует в
фактическом `syncable` и в шаблоне генератора 801. Сейчас он не загружается.

**Отдельные participant audio:** `Meeting` содержит только
`Records []Record json:"recording_files"`. Поля `participant_audio_files` нет.
Оно не декодируется/не сохраняется независимо от списка syncable. Одного
добавления `audio_only` в YAML недостаточно для дорожек участников.

`recording_type=audio_interpretation`, остальные видео, summaries и прочие
неперечисленные типы также не сохраняются. Это фильтр, не полный архив Zoom.

## 4. Имена и метаданные

`Record.Paths` создаёт каталог `<repository>/<DateTime[:10]>/<record.Id>`.
`DownloadRecord` передаёт этот каталог библиотеке `grab/v3` и сохраняет её
`resp.Filename`. Сам zoomrs не конструирует `GMT..._Recording...`.
Точный выбор имени выполняет библиотека по HTTP-ответу/URL; сетевой ответ
и исходники зависимости в этой проверке отдельно не исследовались.

Фактически в БД есть имена вида:

```text
2026-10-16/<video-record-id>/GMT20261016-130852_Recording_avo_640x360.mp4
2026-10-16/<vtt-record-id>/GMT20261016-130852_Recording.transcript.vtt
```

Схема SQLite records сохраняет ID, meetingId, type, startTime, extension,
размер, приватные download/play URL, локальный статус и path. **Не сохраняет**:

- `recording_end`;
- исходный `file_name` участника и принадлежность participant audio;
- timezone/исходный RFC3339 offset;
- полный оригинальный API inventory и полноту экспорта;
- контентный hash и подтверждение комплекта.

Go перед сохранением вызывает `.Local()` и записывает `time.DateTime` без зоны.
Зона зависит от окружения контейнера; по одной строке SQLite нельзя утверждать
UTC или MSK. В API-модели нет recording_end вообще. Это недостаточный контракт
для надёжной синхронизации отдельных дорожек/сегментов.

В одной наблюдённой встрече meeting start в 801 — 13:02, а file recording start
в имени/БД — 13:08:52. Уже существующие данные показывают, что начало встречи
и начало записи не обязаны совпадать. Текущий `_meetings()` 801 читает только
meeting_start, не startTime файла: будущий адаптер должен сохранять оба.

## 5. Синхронизация и дозагрузка

- `client.GetIntervalMeetings` вызывает `/users/me/recordings` с from/to,
  page_size=300 и next_page_token. Это не Get meeting recordings по UUID.
- При включении `SyncJob` сначала делает запрос, затем повторяет примерно раз
  в час, всегда `GetMeetings(ctx, 1)` — один вчерашний день. Старый архив и
  сегодняшний день так автоматически не перебираются.
- `zoomrs-cli --cmd=sync --days N` обращается к одному дню today-N. Патч
  `DownloadOnceOf` ограничивает загрузку/повторы UUID этого дневного пакета.
- **Существующий UUID целиком пропускается:** нет upsert/сверки record IDs.
  Поздно готовый VTT, новый сегмент или включённый новый тип для старой встречи
  не дозагрузится простым повторным sync.
- `SaveMeeting` выполняет INSERT встречи, затем INSERT каждого record без
  единой транзакции. Сбой посередине может оставить неполный состав; последующий
  sync пропустит уже созданную встречу. Нужна транзакция + сверка по record ID.

## 6. Надёжность и границы downloaded

Скачивание последовательное: выбор queued -> status downloading -> `grab.Get`.
Токен получается через client; после HTTP 401 одна попытка с новым токеном.
Статус downloaded записывается после HTTP 200, ненулевого точного размера и
проверки расширения. Контентный hash, media probe и комплектная квитанция отсутствуют.

Вызов `grab.Get` не передаёт context текущей операции. Поведение resume,
таймаутов и временных файлов библиотеки здесь не подтверждено — не считать
их гарантией контракта. Не найдено отдельного атомарного final rename в
`DownloadRecord`: 801 обязан ждать статуса и проверять свою копию.

Когда queued закончились, `ResetFailedRecords` возвращает failed/downloading
в queued. Это не lease/fencing-протокол: сервисный и CLI downloader нельзя
запускать одновременно на одной очереди. В idle service download-loop ждёт
до минуты, при наличии работы — примерно секунду между попытками.

Отключение eviction (`keep_free_space=0`) — локальный патч, который бережёт
старые записи, **не ограничивает размер новой загрузки**. Квоты пакета и
резерв места нужны отдельно. Cloud deletion flags сейчас false; их не менять.

## 7. Что получает 801 сейчас

`zoom_dispatch._meetings(include_video=True)` читает ограниченный список типов;
`audio_only` и participant audio туда не входят. `_local_file` проверяет
downloaded, containment, наличие, размер и settle age.

`zoom_local_worker.select`:

1. исключает встречи VTT-only dispatcher;
2. выбирает первый **непустой тип** в порядке speaker-view -> gallery-view ->
   active-speaker, а не приоритет генератора zoomrs;
3. требует ровно одно видео этого типа и ровно один VTT;
4. создаёт один local MP4+VTT job, исходники не трогает.

Поэтому существующий путь — безопасно ограниченный MP4+VTT intake, но **не**
универсальный Zoom bundle downloader или multi-track consumer. Отсутствующий
VTT блокирует этот маршрут даже при пригодном аудио; NAS-контракт может иметь
другую политику без неявного изменения prod.

## 8. Рекомендации к границе producer -> 801

Внешний производитель READY-пакетов должен предоставить:

- UUID экземпляра встречи, record IDs, роли, UTC start/end, исходные имена и
  соответствие относительным путям;
- mixed original audio и, если существуют, полный доступный набор participant
  audio с пометкой групповых/телефонных каналов;
- сегменты Stop/Start и доступные VTT/чат, не только один видеоракурс;
- инвентарь с hash и явной оценкой полноты; отсутствие данных не выдавать за
  доказанную полноту. READY публикуется после завершения пакета.

Для zoomrs это потребует отдельной доработки его модели/API/сохранения:

1. Participant audio: проверить доступность полей выбранного endpoint,
   при необходимости читать detail endpoint по UUID; не рассчитывать на YAML.
2. Транзакционный upsert по meeting UUID + record ID, сверка поздних записей
   без сброса уже скачанных и без автоматического удаления.
3. Сохранение UTC start/end и participant metadata без потери timezone.
4. Ограниченная загрузка по согласованному списку/байтовому бюджету,
   собственность downloader и проверенные копии.
5. Экспорт контракта/пакета отдельно от сырого record-ID repository.

Это требования к интеграционной границе, **не разрешение** менять downloader,
переносить архив или включать фоновые jobs. GPL-код zoomrs остаётся отдельным
проектом; его реализацию не копировать в MIT-репозиторий 801.

## 9. Доказательства / воспроизводимость

Проверены read-only файлы в `/home/clessd/zoomrs` при HEAD `d764479`:

- `storage/model/models.go` — Meeting, Record, Paths;
- `client/client.go:GetMeetings/GetIntervalMeetings`;
- `repo/repo.go:SyncJob/SyncMeetings/DownloadJob/DownloadRecord/freeUpSpace`;
- `storage/sqlite/storage.go:SaveMeeting/saveRecord/GetQueuedRecord/ResetFailedRecords`;
- `cmd/cli/main.go`, `cmd/service/main.go`, Dockerfile, go.mod.

Со стороны 801: `scripts/zoomrs_config.py`, `src/zoom_dispatch.py`,
`src/zoom_local_worker.py`, `docs/zoom-dispatch.md`.
Выполнена безопасная выборка флагов YAML и read-only SQL схемы/типов/путей.
Credentials и download/play URLs в публичную спецификацию не включены.
Тег образа/состояние контейнера проверены через docker inspect; бинарник не
сравнивался побитово с локальным исходником, выводы о коде привязаны к HEAD.
