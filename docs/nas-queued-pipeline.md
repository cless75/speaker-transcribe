# NAS intake and independent ASR pipeline

Version: 0.1.3 · Status: design draft · Project: 801

## 1. Scope and owner decisions

The new archive route starts with prepared directories on NAS. Copying from
F:, organising the archive and implementing its preparation workflow are out
of scope. Existing Drive and local Zoom routes remain supported.

Owner decisions:

- `org/801-archive-input-nas-contract-boundary` — archive input starts on NAS;
  archive results are delivered beside the source.
- `org/801-nas-ready-versioned-results-quality-review` — directory plus `READY`,
  versioned `.asr` results, degraded results delivered with `needs_review`.

Owner refinements: support Zoom bundles and prefer separate participant audio
when available; changed content with an existing READY is a new automatic
revision. See [Zoom bundle research/specification](zoom-recording-bundle-spec.md).
Other details below remain implementation proposals, not additional owner decisions.

This is not the Drive-to-NAS archive pilot in `hub_archive.py`. It neither
removes originals nor sends anything to Drive trash.

## 2. Independent components

```text
Drive scanner ─┐
Zoom scanner ──┼─> local durable queue -> stager -> ASR worker -> publisher
NAS scanner ───┘           ^                |          |            |
                          └──────── stage receipts / recovery ───────┘
```

- Scanners commit each validated candidate as soon as discovered, without
  waiting for the rest of the scan. No scanner takes the GPU lock.
- Stager copies and verifies inputs into local storage using byte-based limits.
- One ASR worker initially owns the GPU. After durable ASR completion it takes
  the next ready job immediately, without waiting for publication or discovery.
- Publisher verifies delivery and commits completion independently of ASR.
- A job failure is a job state transition, not a reason to exit the daemon.
  Invalid node configuration, queue corruption and incompatible schemas are
  component failures and must stop that component visibly.

Processes use separate SQLite connections on Lenovo's LOCAL filesystem, WAL,
busy timeout and short transactions. Neither the DB nor its WAL lives on CIFS.
Large file copying, hashing, network calls and ASR occur outside transactions.

An optional local notification wakes idle workers; SQLite is authoritative.
A bounded idle poll (proposed maximum 2 seconds) remains the fallback so a lost
notification cannot stall work. A ready job must be claimed within 5 seconds
when the worker, lock and disk budget are available.

## 3. NAS input contract — proposal v1

### Producer responsibilities

1. Build a dedicated directory for one recording/meeting under an enabled
   collection root. File names may contain Unicode and spaces.
2. Finish copying every member and close files. Avoid editing inputs while a
   snapshot is being taken; 801 must detect instability rather than assume it away.
3. Write an empty `READY.tmp`, close it and rename it to `READY` last.
   Merely appearing or having an old modification time does not make media ready.
4. Corrections under the same READY are permitted and create automatic content
   revisions after revalidation. An older running snapshot is never silently
   rebound to new bytes. Do not move/delete source directories during delivery.

801 does not create, consume/delete or rename the producer's `READY` marker.
The initial contract permits only an empty regular READY file. Directories,
symlinks and nonempty control files are rejected; input never contains commands.
No node config, model path, executable or absolute output path comes from READY.

### Composition — Zoom bundles

- Multiple media files are allowed: participant audio, mixed audio, alternative
  video layouts and sequential recording segments. Classification and selection
  follow `zoom-recording-bundle-spec.md`, not a one-media-per-directory rule.
- Prefer separate participant tracks with verified membership, coverage and
  timeline; otherwise select mixed original audio or a verified video fallback.
- VTT/chat roles are frozen in inventory; same-stem names are not mandatory
  across Zoom exports. Chat is never an instruction to the worker.
- Ambiguous membership, timing or roles hold the submission with a specific
  reason. File names alone cannot establish a ready processing plan.
- `Audio Record/` is an allowed participant-track subdirectory, not skipped as
  it is in the existing Hub adapter. Other nested directories are not silently
  folded into a meeting. Scanners can find
  distinct ready directories beneath the collection; nested READY submissions
  are held until composition rules are explicit.
- Exclude READY, temporary control files and generated `*.asr` directories from
  input and revision hashing. Never discover transcripts as new ASR sources.
- Reject symlinks, traversal, escaped paths and case/Unicode-normalisation
  collisions that would be ambiguous on the configured filesystem.

This conservative rule avoids applying the legacy timestamp heuristic to an
archive without knowing which audio/video is primary. Future multi-track
selection is a versioned contract extension, not an implicit guess.

### Collection configuration (node-owned)

Each collection has a stable collection ID, explicit enabled flag (default
false), allowed NAS root, expected mount identity, ASR profile, priority class
and local-buffer policy. Unknown collections and profiles fail closed.
Mount verification precedes reads and writes; an unmounted NAS directory must
not be treated as an empty collection or a writable local destination.

Do not reuse the archive pilot's NAS destination/identity automatically: the
input collection and its allowed write area require separate configuration.
Paths and credentials are node-local, not embedded in the public example.

### Validation and identity

Discovery performs cheap validation and records a candidate, NOT an accepted
immutable revision. The stager streams members while computing SHA-256,
checks the original listing/stat before and after copying and revalidates READY.
Changed membership or bytes during capture prevents transition to ready and
triggers a fresh snapshot. Stable changed content becomes a new revision even
with the old READY. Stat equality alone is not content verification; periodic
full hash reconciliation is needed for replacements preserving metadata.

- Submission ID: stable collection ID plus relative ready-directory path.
  READY generation is an observation token, not a revision key
  or proof of immutable bytes; unchanged READY does not suppress rescans.
- Content revision: SHA-256 of a canonical inventory sorted by relative path,
  including member path, role, byte length and content SHA-256.
- Processing identity: content revision plus canonical processing-profile
  digest and engine release identity. Labels alone do not identify a model.
- Queue idempotency: one job per submission/content revision/processing identity;
  rediscovery never resets progress or retry backoff. Hash-identical source in
  another directory may share verified computation only through an explicit
  cache policy; it still requires a distinct destination receipt.

Store the complete frozen input inventory locally. Do not calculate the full
archive's hashes before processing its first job: hash incrementally as staging
capacity becomes available.

## 4. Queue and stage ownership

Evolve the role of `unified_intake.Queue`, not `jobs_queue.py` (the latter is
the existing shared Hub maintenance-command mechanism). Introduce a versioned
pipeline schema and adapter API alongside the old schema before cutover. Do
not alter the live DB in place as the first experiment.

```text
discovered -> staging -> ready -> transcribing -> publish_pending
           -> publishing -> done

stage error -> retry_wait(resume_stage) OR held(reason)
operator pause -> no new stage claims for that batch
```

Quality is orthogonal: `ok`, `degraded`, `unknown`; review is `needs_review`
for degraded output, never silently `accepted`. `done` means verified delivery.

Required durable fields:

- job ID, adapter, submission ID, batch, collection/project, locator;
- frozen inventory/revision, profile digest, engine identity, destination policy;
- status, resume stage, priority class, creation/eligibility timestamps;
- per-stage attempts, retry time, structured private error and aggregate reason;
- lease owner, random fencing token, lease expiry and heartbeat;
- local input/result receipt references and delivery receipt;
- reserved bytes and actual local disk usage.

Claims use a short `BEGIN IMMEDIATE` transaction and compare-and-set from an
eligible state. Every heartbeat, completion or failure update requires the
current fencing token; a stale process cannot complete another process's job.
Use bounded retry backoff for transient I/O. Ambiguity, checksum disagreement
and unsupported configuration are held for review, not retried indefinitely.

An expired lease triggers reconciliation, not immediate concurrent re-execution.
For GPU stages first prove the previous process no longer owns the shared lock.
If the old process is alive but unhealthy, report blocked ownership for operator
action; do not steal the GPU or kill another production worker implicitly.
Filesystem side effects also require stage resource locks: a DB fence alone
does not stop a stale process writing files.

## 5. Durable stage receipts and crash recovery

SQLite owns scheduling and leases. Versioned, atomic local receipts own evidence
of finished stages. Each receipt contains job identity, stage, input revision,
profile/engine identity, output inventory and checksums. Flush local receipt and
files before committing the DB transition. Private logs stay local.

On recovery reconcile receipts under job ownership:

| Failure point | Recovery |
|---|---|
| Partial download | Discard/reuse only verified chunks; never promote partial input |
| Input receipt durable, DB still staging | Verify receipt/files, promote to ready |
| ASR output present, no complete receipt | Engine-specific safe resume or retry; presence alone is not completion |
| ASR receipt durable, DB still transcribing | Verify identity/files, promote to publish_pending, no repeated ASR |
| Partial NAS delivery | Resume delivery from local verified output, no repeated ASR |
| NAS completion receipt exists, DB not done | Read back and verify delivered inventory, then mark done |
| Source changed or existing version conflicts | Hold; never silently overwrite or relabel the result |

Resume of valid processed results is pinned to the engine identity that produced
them. Updating a release does not justify relabelling an old result as a new E2E.

## 6. Output beside the primary source

Proposed layout (full source name, including extension, avoids audio/video collision):

```text
meeting/
  recording.m4a
  recording.vtt
  READY
  recording.m4a.asr/
    <processing-identity>/
      transcript.md
      transcript.txt       # when produced by the selected profile
      subtitles.vtt        # when produced by the selected profile
      quality.json
      receipt.json         # completion marker, written LAST
```

`quality.json` records measured quality and `needs_review` for degraded output.
`receipt.json` binds source inventory, profile, engine, quality and delivered
artifact lengths/SHA-256. No `latest` pointer or replacement of user files is
required in v1. Absence of a requested mandatory output holds delivery; optional
formats are described by the profile, not invented by the publisher.

Write to a per-attempt temporary directory, read back all delivered artifact
checksums and publish the final directory/receipt under exclusive destination
ownership. Do not assume local POSIX rename/fsync semantics on CIFS. Validate
the exact NAS mount's behaviour in the pilot; the portable visibility rule is
that a version is complete only with a valid LAST-written receipt. After an
ambiguous network error inspect the destination before repeating publication.

An identical verified final version is idempotent success. A differing final
version at the same identity is a conflict, not permission to replace it.
Publisher rechecks source identity before delivering beside it: a replacement
source must not receive a transcript for the previous bytes.

## 7. Backpressure, scheduling and retention

- Reserve disk bytes transactionally before staging; concurrent stagers cannot
  each consume the same free space. Count staged input, audio expansion, ASR
  intermediates and undelivered output. Recheck actual free space during work.
- Configure explicit byte high/low watermarks and minimum free reserve. If full,
  stop claiming more input; do not delete originals or unverified outputs.
- Start with one GPU consumer and one stager/publisher each. Tune concurrency
  after measurements; network hashing must not overload the NAS.
- Weighted service between operational and archive classes prevents archive
  starvation. Proposed initial ratio 3:1, FIFO within class; a single active
  class uses all slots. Batch pause blocks new claims, not implicit cancellation
  of a live ASR. Duration estimates improve reporting, not safety.
- No new cleanup defaults in this change. Keep current 30-day post-verified
  publication rule for existing Hub copies. NAS-job local retention is explicit
  node policy to settle before a large pilot; do not silently reuse Hub cleanup.

## 8. Compatibility and deployment

Windows-legacy and Linux-current remain equal supported deployments. Queue and
validation code is platform-neutral; lock, service and path adapters are chosen
by deployment profile. Initial GPU deployment is Lenovo, not a Windows migration.
Do not import the unfinished platform layer from the main checkout implicitly.

Drive identity remains ID/revision; Zoom identity and bundle fingerprints stay
unchanged. Their adapter receipts preserve current claims, VTT ownership and
quality checks. Source errors must not masquerade as an empty queue.

The production shared `.worker.lock` remains mandatory for all GPU consumers.
During development the new pipeline is disabled and uses isolated local state.
Never run it as a second GPU consumer beside unified-intake. Before actual
cutover: stop admissions from the old service, let its active job finish, migrate
and reconcile journals read-only, verify locks/leases, run same-release E2Es,
attest and only then enable the new consumer. Rollback also drains/fences the
new consumer before restoring the old one. No SQLite schema downgrade in place.

Component attestations pin their dependency closure and queue compatibility,
not only the component's top-level file. Scanner changes must not invalidate
ASR unnecessarily, but shared worker/model changes still require relevant E2Es.
Implement this explicitly; removing the existing gate is not the migration.

## 9. Health and acceptance tests

Health separates service liveness, discovery completion, stage progress, delivery
and review. Show queue counts/age by stage and batch, GPU idle-with-ready-work,
disk pressure, NAS availability, retries, held reasons and per-component release
gate. Public logs use IDs and aggregates; private errors may contain paths.

Required tests before enabling real input:

1. Block a scanner mid-scan: a previously committed ready job still runs.
2. Enqueue while worker idle: claim within 5 seconds; next ready job starts
   without scan/publication completion.
3. Two consumers race: one lease winner; stale token cannot mutate state.
4. Expired GPU lease with old process alive: no concurrent ASR execution.
5. Crash at each receipt/DB boundary: no lost job or repeated completed ASR.
6. READY absent, malformed or source changing: never accepted as ready.
7. Unsafe paths, symlinks, Unicode/case collisions, ambiguous bundles: held.
8. Same submission rediscovered: no duplicate; new bytes/profile yield new
   identity and output version without overwriting previous results.
9. NAS disconnect/unmount during read or write: bounded retries, no local
   mountpoint fallback, no false completion; recover delivery independently.
10. Degraded output: delivered, done+needs_review, no automatic quality acceptance.
11. Backpressure with two stagers: reservations stay within configured budget;
    crash reconciliation releases only obsolete reservations.
12. Existing Hub and Zoom receipt migrations/E2Es retain claim and publication
    guarantees; no competing GPU consumer during cutover.

## 10. Implementation evidence and owner roadmap

The owner-facing release plan is maintained in the project vault:
`cless75-ex1/areas/60-techplatform/801-asr-engine/801-p10-NAS-Queued-Pipeline-Release-Plan.md`.
Do not maintain a second editable release roadmap here. This repository owns
technical contracts, implementation and tests.

As of 2026-10-27:

- Initial I1 stage runtime, local receipts/resource locks, transactional byte
  reservations, guarded NAS copies, dual-delivery protocol and read/copy-only
  Hub source adapter are implemented in isolation. See
  [I1 interfaces, limitations and safety gates](hub-nas-i1.md). Real ASR/Hub
  publication/claim adapters and production services are NOT connected.
  An exact pilot-project exclusion is available in the old intake, but its
  long GPU critical section still needs a safe split before mixed-route use.

- `src/pipeline_queue.py`: isolated SQLite schema v1, fenced claims, leases,
  heartbeat, retry/held and explicit evidence-based expired completion. Runtime
  consumers and reservations are separate libraries; weighted scheduling,
  incomplete-stage recovery and production migration are not implemented.
  `finish()` is not stage evidence: the adapter must verify a durable receipt first.
- `src/zoom_bundle.py`: read-only READY/inventory/hash/path validation and
  conservative media/timeline eligibility, tested on Windows and Linux.
  Multi-track alignment/coverage and overlapping-video choice remain held
  without supporting evidence. See the bundle specification for test results.
- The separate GPL downloader exports complete observed local Zoom inventories;
  this does not implement NAS transfer or the new ASR consumer.
- Production still uses the existing intake; the independent pipeline has not
  been enabled. Source contracts remain subordinate to shared GPU ownership.

Open before NAS pilot: exact collection roots/mount identity, allowed write
areas, Zoom timeline/coverage fixtures, profile/output set, byte budgets and local NAS-job
retention. Open before production cutover: deployed adapter completeness,
dependency pins, migration/rollback evidence and E2Es. These are not guessed
from the old archive-pilot config.
