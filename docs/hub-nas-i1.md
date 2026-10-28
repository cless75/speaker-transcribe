# Hub → NAS: isolated I1 implementation

Version: 0.0.8 · Status: limited I1 pilot accepted; NOT the full pipeline release

The owner roadmap remains in the project vault (`801-p10`). This document
describes technical interfaces and evidence, not a second owner roadmap.

## Implemented

- `pipeline_storage.py`: cross-platform local resource locks; atomic JSON
  receipts with flushed files; frozen processing identity and payload digest,
  exact SHA-256 artifact inventories, upstream receipt chaining, explicit
  quality/review and two-destination delivery requirement.
- `pipeline_budget.py`: separate isolated local SQLite ledger with transactional
  byte reservations, a configured capacity and minimum-free floor. Multiple
  stagers cannot reserve the same free bytes. Configuration drift is refused.
  Reservation accounting is conservative (already-materialised bytes also
  count); no reservation or source is automatically removed.
- `pipeline_runtime.py`: independent long-lived consumers for staging, ASR and
  publication; bounded idle polling, lease heartbeats, fenced completion and
  per-job file ownership. Only ASR obtains the common GPU lock. Handlers are
  injected trusted Python objects, not executable names taken from manifests.
- `PipelineQueue.reconcile_finished`: explicit receipt-only reconciliation of
  expired completed stages, after acquiring job/resource locks. No evidence
  means no promotion or automatic retry. Expiry never grants GPU ownership.
  Any unreconciled transcribing job blocks new GPU claims even if flock is free:
  a dead parent may leave a running child. ASR reconciliation additionally
  requires deployment-provided proof of resource release; a synthetic test
  callback is not a production proof.
- `pipeline_delivery.py`: NAS mount source/target/identity guard before writes,
  versioned copies, SHA-256 read-back BEFORE completion receipt, source READY
  last; existing conflicts are held, not replaced. The Windows guard supports
  explicit UNC share roots, not unverifiable mapped-drive identities.
  Linux findmnt filters for CIFS: a systemd autofs layer at the same target is
  not the mounted share identity and cannot substitute for an actual CIFS mount.
  NAS-owned destination verification compares full SHA-256 and byte length:
  CIFS timestamp/inode attribute refresh after a write must not be interpreted
  as a local-input mutation. Local source/receipt snapshots retain strict
  inode/mtime/ctime checks; destination checksum requirements are not relaxed.
- `DualDelivery`: independently receipted Hub/NAS sinks; failed delivery never
  repeats ASR; a verified sink is rechecked on resume, not blindly trusted.
  The terminal Hub marker finalizer runs only after BOTH destinations verify.
  Recovery of a completed publishing attempt also rechecks both destinations.
- `hub_nas_intake.py`: explicit-project, read/copy-only source adapter; reuses
  current pending-input history/quarantine rules. Freezes Drive ID/revision,
  primary filename, content-based profile/engine identities and NAS destination.
  Exact MD5 verification, source revision checks before/after download, SHA-256
  NAS read-back. Source mirror layout: `sources/<submission-revision-hash>/`
  with source file, receipt and READY; results belong beside that file in
  `<full-source-name>.asr/<processing-id>/`.
- `unified_intake.py`: explicit `excluded_hub_projects` allows routing a pilot
  away from old discovery. The exclusion is exact (no wildcards) and included
  in release identity; adding it does not bypass the old attestation gate.

## Concrete pilot adapters (2026-10-28, bounded E2E verified)

`pipeline_hub_claims.py` maintains token-bound Hub claims across stage gaps with
verified heartbeats. Foreign expired claims are never preempted. Persisted
tokens allow reconciliation of an ambiguous first write. ASR uses a bounded-age
local ownership proof, not a recursive Drive scan; stale ownership aborts only
its own controlled child. The supervisor singleton excludes competing local
coordinators, and the common GPU lock remains unchanged.

`pipeline_hub_adapter.py` connects the real engine and both publishers:

- Explicit source scope, legacy-journal exclusion and source marker/ID checks
  precede copying. Single-media admission holds bundles/unsupported inputs.
- Media-duration admission plus ongoing scratch/log/free-space accounting
  enforce the job reservation. Input receipts remain immutable; the engine
  receives a separate local work input, never the receipted input directory.
- `audio_inbox_watch.run_asr` accepts an optional process controller; the legacy
  path is unchanged. Controlled children run in their own POSIX process group.
  Ownership/disk/lease failure terminates only that invocation, not another
  worker. `pipeline_process.py` records its start and observed exit privately.
- Engine output completion is bound to job identity and a flushed work artifact
  inventory BEFORE snapshot/publication. Interrupted result copying resumes
  verified files without running the model again. Unknown/conflicting files
  are held, never overwritten or inferred complete from mere presence.
- Full declared output files, transcript and run metadata are checked. Quality
  details are preserved; degraded output explicitly needs review.
- Hub results use a new full-processing-ID session suffix, not the old output
  path, so another profile/revision cannot replace an earlier publication.
  NAS results remain beside the immutable source mirror in `.asr/<job-id>`.
- Drive transport errors permit independent NAS delivery; read-back/integrity
  conflicts are held. Terminal source markers are written LAST, only after
  both destinations verify. Restart verification checks real destinations again.
- Completed jobs retain their files. Only unused reservation is settled down to
  retained logical bytes; there is no automatic file deletion/cleanup.

`hub_pipeline.py` runs scanner/stager/ASR/publisher independently, with one
connection/ledger per consumer, bounded idle polling and a private health file.
The scanner uses explicit source policies; E2E admits only the configured exact
source and can pin expected Drive ID/hash/size. Source names never select code.

Prepare → explicit E2E → explicit attest → enable/serve are distinct actions:

```sh
python src/hub_pipeline.py --config /private/pilot.local.json --prepare
python src/hub_pipeline.py --config /private/pilot.local.json --e2e
python src/hub_pipeline.py --config /private/pilot.local.json --attest JOB_ID
# Only after evidence and routing/service approval:
python src/hub_pipeline.py --config /private/pilot.local.json --serve
```

Default example is disabled. Serving pins code/unit closure, Python/package
identity, profile, queue compatibility, routing/budgets and a verified completed
E2E receipt. The enable switch itself does not relabel evidence. Config/profile
drift blocks startup rather than silently regenerating attestations.

The disabled systemd template requires a node-local ExecStart drop-in. A bounded
restart policy plus recovery timer retries a failed component after external
conditions recover, without restarting a healthy active instance every five
minutes. Before serving, old consumer timers must be disabled, not just stopped.
This is the first Linux-current pilot, not removal of Windows-legacy support.

## Remaining hard boundaries before serving

The bounded real E2E is verified/attested and the owner enabled the new service
and recovery timer for the approved pilot scope. The old intake timer is
disabled, not merely paused. **This is not migration of every Hub/Zoom route,
the full NAS archive scanner, multi-track acceptance, or the full crash/reboot
matrix.** The two-discovery-pass service acceptance check passed without
reclaiming/retranscribing the completed source. Completing library tests alone
never bypasses live gates.

Before connecting production handlers:

1. Verify live durable source ownership and explicit legacy-journal exclusion.
   Full import/handoff of unfinished legacy work remains I4. A source copy
   is not an ASR claim. Preserve cross-node claims, VTT ownership, exact markers,
   accepted legacy history and unfinished publication. The old cloud claim
   helper can preempt expired remote claims; do not inherit that as permission
   for automatic GPU takeover or treat a copied source as a new unowned input.
2. Exercise the concrete ASR and publication adapters in a real E2E, with mandatory
   outputs, full quality details and `needs_review` for degraded results. No
   exception during delivery may reset ASR to an unfinished stage.
3. For any future mixed-route operation, shorten the old GPU critical section.
   It currently holds the common GPU lock across discovery, copy, ASR AND
   publication. Project exclusion alone prevents duplicate routing but DOES
   NOT remove that scheduling bottleneck. Do not remove its lock globally:
   retain scheduler/job serialization and lock actual GPU use separately. The
   owner instead stopped the old intake for the first new pilot; no mixed mode
   or implicit restoration of the old scheduler is enabled by this implementation.
4. Apply approved local/NAS budgets and exact pilot scope. The download adapter currently
   uses bounded-request rclone copyto, not a per-byte storage quota. Its node
   admission estimator must cover source, WAV/intermediates, outputs, copies
   and retained data; enforce media expansion bounds before enabling it.
5. Provision the explicitly approved NAS area/identity and test the exact
   mount's read-back/flush/rename semantics. `NasArea` never creates an identity
   or mounts a share implicitly. Local paths must be on local storage; do not
   place queue, reservation DB, receipts or locks on CIFS/UNC.
6. Perform read-only migration report, backups, same-candidate real E2Es,
   dependency-pinned component attestations, controlled routing change and
   verified rollback, without replacing a live DB from a stale backup.

I1 intentionally holds meeting bundles with peers and standalone VTT rather
than silently applying size-based video selection. Full Zoom bundle evidence
and timeline handling belong to I2. Sources and prior results are never deleted.

## Safety model and limitations

Local state is node-owned, trusted and protected from other writers. Path and
symlink checks are not a hostile multi-user filesystem sandbox. NAS guards
are fail-closed rechecks, not an atomic fencing mechanism against a privileged
mount replacement between system calls. Deployment must control mount lifetime,
writers and ownership; network copies remain outside SQLite transactions.

Sink `publish` must never write terminal source markers. `verify` must compare
real content and identity; size alone is insufficient. The finalizer must be
idempotent and verify remote terminal markers on every recovery. Synthetic
test sinks are NOT production evidence or substitutes for real E2Es.

`StageRunner` accepts no budget only for isolated tests. Production adapters
must provide `DiskBudget` and a node-owned admission estimator and call the
supplied check at bounded intervals; an engine subprocess additionally needs
its own expansion/free-space controls. Retry ceiling currently uses the
queue's cumulative job-attempt count, so it is conservative across stages;
per-stage attempt reporting remains a subsequent schema change.

## Tests

```sh
python -X utf8 -m pytest tests -q
```

New tests exercise real file/SQLite operations (Windows and Linux), a memory
Drive, and an isolated fake mount identity. Concurrency is checked with blocked
stager/publisher and simultaneous ASR; idle wakeup, shared lock exclusion,
heartbeat, post-receipt failures, expired leases, disk admission, both-destination
retry, input/output corruption, source-state changes, source-history exclusion,
READY timing and absence of Drive writes are tested. These tests do not start
the ASR model, alter live timers, or prove deployment-level GPU ownership.

### Verified development evidence

Concrete adapter candidate `680ffc3`: **373 passed / 2 skipped** on Windows;
**375 passed** on Linux. Includes real controlled-child subprocess tests,
foreign-expired-claim fencing, concrete memory-Drive → NAS → engine-fixture →
dual publication, crash after durable engine completion, each destination
outage, exact E2E source filtering and attestation/startup gates.

A bounded live E2E completed for one owner-selected, previously unmarked source
of 3834.107 seconds: all three stage receipts, both Hub/NAS content read-backs
and the LAST source marker were independently verified. 87 result artifacts,
139395542 bytes; worker returncode 0 and resource release proof confirmed.
Quality is **degraded / needs_review**, gaps 207.22 seconds against a 30-second
threshold, no suspect-loop duration. Publication is not owner quality acceptance.

The queue made four stage claims. There were two ASR starts, but only one
confirmed output completion: an unfinished first attempt was retried
automatically. Do not describe this as exactly one engine invocation. The
specific first retry cause was not retained by the mutable queue reason field;
durable per-attempt event auditing remains an explicit follow-up. The completed
result was not rerun for delivery. Retained logical bytes 483991508 stay counted;
only unused reservation was released, no source/result cleanup was enabled.

The same-candidate attestation was issued; the owner installed/enabled the new
service+timer. The new supervisor loaded the persisted done job without
reclaiming it. Old intake is inactive/disabled, downloader remains active.
Two normal automatic discovery passes completed with no source quarantine or
component failure; the completed source remained done with unchanged claim/start
counts. The limited I1 gate is accepted. No reboot/fault-injection acceptance,
full legacy migration, arbitrary NAS archive intake or multi-track release is
implied. Prior processed Hub files were not bulk-migrated or removed.

Earlier foundation/storage candidate:

Code candidate `abcde0f`:

- Windows: **357 passed, 2 skipped** (existing symlink privilege/case-sensitive FS skips).
- Linux: **359 passed** in an isolated detached worktree, using the node's ASR
  Python plus the existing test dependency directory, without modifying the
  production checkout or services.
- An explicitly approved new NAS area was provisioned with a separate identity.
  A 32044-byte synthetic silent WAV was mirrored, SHA-256 read back, receipted,
  and exposed with READY last. A synthetic non-ASR result beside it in `.asr`
  was also read back. Repeated source/result publication was idempotent.
- Live probe found the autofs/CIFS layering and post-write CIFS attribute
  refresh behaviours described above; regression tests cover both fixes.
- These are storage-integrity probes, NOT speech recognition E2Es. No real Hub
  media was transferred, no source was deleted, and no service was switched.
- Live Drive state reads failed transiently during read-only candidate audit;
  no yet-unprocessed production E2E source has been verified. Source selection
  must not infer queued state from an unreadable marker or stale cache.
