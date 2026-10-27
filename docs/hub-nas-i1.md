# Hub → NAS: isolated I1 implementation

Version: 0.0.2 · Status: development, NOT a production cutover

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

## Hard boundaries

The source adapter has **no real ASR handler, Hub publisher/finalizer, shared
claim implementation, production CLI, enabled service or provisioned NAS
identity attached**. It can copy sources but cannot itself enable execution.
The runtime is an executable library tested with synthetic handlers, not an
operational release. This change is not a completed I1.

Before connecting production handlers:

1. Implement durable handoff of legacy journal/source ownership. A source copy
   is not an ASR claim. Preserve cross-node claims, VTT ownership, exact markers,
   accepted legacy history and unfinished publication. The old cloud claim
   helper can preempt expired remote claims; do not inherit that as permission
   for automatic GPU takeover or treat a copied source as a new unowned input.
2. Attach real source-specific ASR and publication adapters, with mandatory
   outputs, full quality details and `needs_review` for degraded results. No
   exception during delivery may reset ASR to an unfinished stage.
3. Shorten the old scheduler's GPU critical section for a mixed-route pilot.
   It currently holds the common GPU lock across discovery, copy, ASR AND
   publication. Project exclusion alone prevents duplicate routing but DOES
   NOT remove that scheduling bottleneck. Do not remove its lock globally:
   retain scheduler/job serialization and lock actual GPU use separately.
4. Approve local/NAS budgets and pilot source. The download adapter currently
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
