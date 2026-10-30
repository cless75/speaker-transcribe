---
artifact_type: review
version: 0.0.2
requested_by: owner
reviewer: opus-read-only
reviewer_model: opencode/claude-opus-5-5
author: codex-sol
author_model: m4-ccr/gpt-6.1-sol
owner_project: 801
subject: "Read-only package501 0.0.2 compatibility, code4ae8e6f"
roles: [adversary, operator, regression, gate-keeper]
status: answered
opened: 2026-10-30
closed:
unit: 801-W2-501
unit_form: dor-closed
outcome:
return_reason:
---

# 501 package0.0.2 — независимый review

## Запрос

Владелец явно разрешил отдельного read-only Opus-reviewer и продолжение по находкам. Task `801-501-C4`; координатор/единственный writer code/плана/review-файла — parent Sol. Ревьюер ничего не пишет в repo/vault/NAS, возвращает ACK/findings и воспроизведённые проверки текстом; координатор сохраняет их с атрибуцией. Проверочные тесты — только synthetic/temp, без новых ASR/jobs/READY/services/enrollment. Изменение файлов тестовым runtime cache допустимо только в temp, не в live state.

Repo/worktree: `D:/work/wt/speaker-transcribe/nas-queued-pipeline`, branch `feat/nas-queued-pipeline`. Baseline `66afe9dbf32cd60dd72698c82863eb06afbbba98`, candidate `4ae8e6fcb27bdf8877ec507e2f5bd945c4367555`. Предмет: delta `src/nas_pilot.py`, `tests/test_nas_package_check.py`, `docs/nas-admitted-pilot.md`; существующие helpers/old execution path читать для regression. Этот review-файл добавлен после candidate, code SHA не подменяется HEAD.

План: `D:/work/cless75-ex1/areas/60-techplatform/801-asr-engine/801-pi1-w2-501-Package-Compatibility.md`0.0.2 (vault commit `bd19f0ad`); общий PI0.0.6. Ответ501 a1/0.0.2 закреплён commit `db85ea9805b3230c7b1137a88bef3a837e7abf20` в `D:/work/ms-school/boks/501-spm-product`. ТЗ0.0.1/`4a4131f6dc794df6312f078feb36c9a70375b817` не менялось. Точные package/source/run hashes — в плане; exact NAS не обязателен для reviewer: факт доступа/непроверки явно назвать. При доступе — только read-only, не запускать old `--preflight`/`--run-pilot`, не создавать новую конфигурацию/пакет на NAS.

Контракт review: `D:/work/cless75-ex1/concepts/ops/contracts/agent-review-contract.md`; R-3: `D:/work/.claude/rules/parallel-coordination.md` и указанный канон. G2/ACK до работы; при недоступном SHA/входе — blocked(input), без подмены. Роли выполнять отдельными проходами: adversary — опровергнуть accepted/reuse/held и read-only; operator — живые synthetic прогоны, negative cases, отсутствие side effects; regression — старый single0.0.1 и default execution; gate-keeper — совместимость не execution/semantic acceptance, private ownership, unknown/off и boundaries.

Авторский self-check: Windows487passed/2skip, Linux489passed; exact NAS0.0.2 `accepted`, first reuse40outputs/43271743bytes, second held; protected live files unchanged. Это не independent evidence. Не требуется улучшать стиль ради стиля: finding = ID, Blocker/Major/Minor, файл:строка, проверяемое последствие, воспроизведение и рекомендуемая граница правки. Если нашли Major/Blocker — после авторских вердиктов и предъявленного SHA самостоятельно выполнить G4v в этой же reviewer-сессии; её запуск заранее разрешён владельцем, отдельного подтверждения на каждый виток не нужно.

## Находки

### Раунд1 — Opus, 30.10.2026

Источник — ответ reviewer-сессии `ses_e7213c159ffeInKQ49YKi3Vf87`, модель `opencode/claude-opus-5-5`; сохранено координатором с атрибуцией. Ревьюер ACK exact candidate4ae8e6f/baseline66afe9d, G2-противоречий не нашёл. Устаревшее «reviewer не назначен» в плане отмечено для исправления. Ограничение процесса: R-3 не был прочитан reviewer; перед G4v требуется его чтение/ACK, findings от этого не становятся author self-check.

Independent operator: Windows487passed/2skip (`PYTHONDONTWRITEBYTECODE=1`, pytest `-p no:cacheprovider`); собственные probes через `C:/Users/corne/AppData/Local/Temp/opencode/c4probe/probe.py`. Для probes использован настоящий synthetic0.0.1 pipeline с MemoryDrive/engine и тремя stage receipts, не только hand-written completion автора. S0: v1reuse/v2firstreuse+secondheld; old Admission0.0.2 отклонён, oldpreflightcompleted held. S1: лишняя редакторская копия вrun принята checker, но штатный NasArea._verify отвергает. S2: missing/foreign marker принят. S3: legal empty output отвергнут. S4: malformed types→TypeError. S5: casealias exclude обойдён. S6: v1secondaryscope принят. S7–S10: partial.asr blocked, пустой.asr held, samebytes второго held, переименованный source_id blocked.

Independent exact NAS: штатный verify_mount/UNC, manifest1dd5d710…3a3f3,6members accepted; first original job/manifest/receipt,40outputs/43271743bytes; second held, no adjacent.asr. Snapshot57записей до/после sizes/mtime совпал, текущие outputs не содержат лишних/пустых файлов; следовательно S1–S3 не опровергли текущий exact вердикт, но выявили общий дефект проверки. Reviewer не воспроизводил Linux suite/protected live state/Hub-delivery/semantic acceptance; не считал их пройденными. Обнаруженные repo gitignored pycache не были достоверно атрибутированы, поэтому абсолютное отсутствие cache-записей не заявляется; corpus read-only подтверждён отдельным снимком.

| ID / уровень | Адрес в candidate4ae8e6f и проверяемое последствие | Роль |
|---|---|---|
| **C4-M1 · Major** | `src/nas_pilot.py:480–492`: проверяются только перечисленные receipt файлы, лишние файлы run не ищутся. Расходится с `pipeline_delivery.py:88–105`; changed raw-tree ошибочно подтверждается как reuse. ProbeS1 | adversary/operator |
| C4-m2 · Minor | `src/nas_pilot.py:449–460`: source-bound collection marker не проверяется; missing/foreign marker даёт reuse. ProbeS2. Проверять schema/collection_id, не сравнивать Linux mount_source с Windows UNC буквально | adversary |
| C4-m3 · Minor | `src/nas_pilot.py:486`: size<=0 блокирует легальный zero-byte receipted artifact, допустимый штатной delivery. ProbeS3 | regression |
| C4-m4 · Minor | `src/nas_pilot.py:470,483–485,555–558,563–565`: malformed input types дают TypeError/KeyError вместо EvidenceError, искажая классификацию отказа. ProbeS4 | operator |
| C4-m5 · Minor | `src/nas_pilot.py:561,566–568`: case-sensitive excludes можно обойти на Windows/CIFS другим регистром. Execution не открывается; ProbeS5 | regression |
| C4-m6 · Minor | `src/nas_pilot.py:574–583`: version не связана со scope/held combination, v1secondaryscope и некорректные v2комбинации accepted. ProbeS6 | gate-keeper |
| C4-m7 · Minor | `src/nas_pilot.py:611`, `docs/nas-admitted-pilot.md:18`: first blockers=[] скрывает необходимость semantic acceptance; отсутствие adjacent completion неверно названо доказанно unexecuted source без поиска Hub/legacy/другого узла | gate-keeper |

Все роли отработаны отдельно. Reviewer подтвердил read-only нового пути и неприкосновенность старого execution0.0.1; находок Blocker нет. Независимое семантическое принятие/production install не заявлены. При принятии C4-M1 срабатывает G4v того же reviewer.

## Вердикты

| ID | Вердикт автора / изменение |
|---|---|
| C4-M1 | **Принято:** read-only os.walk/no_links и exact inventory+receipt set, unlisted file →blocked; отрицательный test_C4_M1. Исправление не разрешает удалять лишний файл |
| C4-m2 | **Принято:** schema/collection_id source-bound marker проверяются; mount identity по-прежнему проверяется отдельно штатным guard; tests missing/foreign/schema |
| C4-m3 | **Принято:** допускается size=0, отрицательный размер запрещён; regression с zero-byte receipted artifact |
| C4-m4 | **Принято:** public check_package преобразует malformed input KeyError/TypeError/AttributeError/JSON ValueError в EvidenceError, существующие EvidenceError сохраняет; tests по5формам |
| C4-m5 | **Принято:** NFC/casefold сравнение excludes/roots/source aliases консервативно для shared CIFS-корпуса; tests3alias forms |
| C4-m6 | **Принято:** format-specific scope/held pairs, v1толькоinitial; v2validation initial/secondary привязана к scope; positiveoldformat и negativev1/v2матрица |
| C4-m7 | **Принято:** per-source completion_evidence_scope=adjacent-nas-only, external_completion_search=not-performed, semantic_acceptance=not-assessed; reuse несёт shared_semantic_acceptance_required. Документ не утверждает «никогда не исполнялся» из отсутствия.asr |

Все7находок приняты. Порог G4v выполнен; исправления будут предъявлены exact SHA, файл остаётся answered до G4v/P. Первичный Windows full regression после исправлений:504passed/2skip. Не закрывать review по одному этому self-check; человеческое semantic acceptance501 остаётся HELD.
