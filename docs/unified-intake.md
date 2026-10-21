# One Lenovo intake: Drive media + local Zoom bundles

The Lenovo service `unified-intake@clessd` is the **only automatic ASR
consumer**. It reads Drive IDs/revisions directly (not the Hub FUSE listing)
and zoomrs' local SQLite/download repository. Drive media is staged and hashed
on Lenovo before ASR; Zoom MP4 is staged locally and its VTT supplies speaker
names. The Zoom MP4, ASR WAV chunks and private logs never go to Drive.
Both routes verify every published file and write `asr-done` last. A completed
`degraded` transcript is published with its quality warning, not labelled `ok`.

## State, queue and gates

`out/hub-worker/unified/queue.sqlite3` indexes both sources and stores fair
alternation, retry backoff, and reasons for failed attempts. **Existing per-job
journals are authoritative** for resuming ASR/publication; `--migrate` indexes
them without modifying them. One pass attempts at most three inputs across
both routes. The Hub route retains its shared cross-node claim; both routes
hold the same Lenovo lock while executing. A failed Drive project scan does
not mark the queue empty or erase Zoom inputs. Neither source is deleted.

The 24-hour pilot receipt is replaced by a **release attestation**, not an
unconditional bypass: `--attest` requires a fresh same-code Hub media E2E and
a Zoom media+VTT E2E after `deployed_at`. The attestation pins the worker code,
systemd templates, node configs, routing, and host. Each pass checks the Hub
proof still has a verified source marker and the Zoom proof still matches
published output. Meaningful code or config changes block the next pass until
the release is re-attested. Expiration by calendar time alone does not.

## Operator cutover (Lenovo)

1. Install the reviewed release commit. Create gitignored
   `config/unified.local.json` from `config/unified.example.json` with the real
   paths and **timezone-aware release deployment timestamp**. Keep it mode 600.
2. Disable the old `hub-worker@clessd.timer`, the optional
   `zoom-local-worker@clessd.timer`, the VTT-only `zoom-dispatch@clessd.timer`,
   and `speaker-transcribe-watch.timer`. **Wait for the active services to
   finish by checking their PID**, not `systemctl is-active --quiet` (a oneshot
   in `activating` can be mistakenly taken for finished).
3. Run `python src/unified_intake.py --config config/unified.local.json
   --migrate`. Inspect `done`, `resume`, `held` counts; investigate holds before
   cutover. Repeat it to check idempotence. Dry-run without flags reports
   eligible inputs without running ASR.
4. Run a new Hub media canary through `hub_worker.py` on this exact release.
   Run one new local Zoom MP4+VTT on this exact release and verify that only
   text/quality/state reach Drive. Inspect the original source/marker and the
   session's `pipeline/state.json` independently. Neither E2E may reuse a
   `done` journal from before deployment.
5. Run `python src/unified_intake.py --config config/unified.local.json
   --attest`; then an explicit `--run --max-files 1` canary. Install
   `scripts/systemd/unified-intake@.{service,timer}` under `/etc/systemd/system`,
   `daemon-reload`, and `enable --now unified-intake@clessd.timer` using the
   operator's sudo. Do **not** re-enable either old timer.
6. Confirm two automatic cycles, an input from each route (if available),
   no duplicate sessions, and that the unified service is not in `failed`.
   A Drive outage or ambiguous Zoom VTT remains visible as incomplete/held.

Rollback: disable the unified timer, wait for or intentionally stop its active
service, preserve `queue.sqlite3` and all journals, then reconcile partial
publications before enabling an old consumer. Do not delete anything in Hub.

## Source copies, retention and archive pilot

Drive sources are downloaded to `out/hub-worker/intake/<project>/<Drive ID>/<revision>/`
and verified against Drive MD5 before ASR. `out/hub-worker/outputs/` holds local
ASR results; verified publication goes to `Hub/<project>/sessions/`. The
current `hub_worker.py --cleanup-local` gate retains the downloaded media for
**30 days after confirmed publication**; it does not remove Hub originals.
An earlier proposal to delete the Lenovo copy immediately is not active yet.

Separately, zoomrs downloads original MP4+VTT to its local repository;
`zoom_local_worker.py` stages another MP4+VTT under `out/zoom-local/jobs/`.
Neither Zoom original nor working copy is automatically deleted by the NAS
archive pilot. Any Zoom cleanup requires its own retention and restore policy.

`config/archive.example.json` is the **disabled** per-project policy for a
possible NAS archive: `keep`, `archive` (Drive `_archive`), or
`trash_after_nas`. The selected pilot for `415` is `trash_after_nas`, but
the example keeps `enabled: false`. The read-only inventory is:

```sh
python src/hub_archive.py --config config/archive.local.json --project 415
```

It lists exact paths, Drive IDs, revisions, sizes and hold reasons. Eligible
rows require a unique Drive source and state marker, matching revision,
`publication_verified: true`, an eligible local job journal, and a freshly
verified published output. Old `asr-done` alone is **not** enough. Timestamped
meeting bundles and current Hub release E2E sources are held. The Python
`copy_verified()` helper can only stage a uniquely eligible, explicitly
enabled source plus its marker and read back SHA-256 from NAS. It requires a
CIFS mount at `/mnt/wd-book` with source `//192.168.1.28/Archive`, a separate
operator-provisioned identity file `.speaker-transcribe-archive` containing
`speaker-transcribe-archive-v1`, and reserved free space. The example does not
create that identity file. The mount root itself is not `/mnt/wd-book/Archive`.

**No archive timer or Hub-mutation command is supplied.** `hub_action` is
policy metadata, not permission to send a file to the Drive trash. The
existing migration and local cleanup still require the live Hub marker; do
not trash sources until archive-aware verification, exact-ID deletion, restore
test, and a separately approved list of files are implemented and accepted.
