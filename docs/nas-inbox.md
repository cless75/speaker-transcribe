---
version: 0.0.1
updated: 2026-11-05
---

# Offline NAS Inbox

`nas_inbox.py` consumes `<heavy>/_inbox/<project>/<meeting>/` using the existing stage runtime, shared GPU lock and shared byte ledger. It never calls Drive. `continuous: true` enables repeated scanning; omit `approved_inputs` to inspect every project/meeting directory. Existing results and unresolved claims remain blocked, not implicitly approved for ASR.

The scanner inventories and SHA-256 hashes all original files, rejecting symlinks, normalization collisions and changing/recent inputs. It chooses Zoom VTT first, then participant Audio Record tracks, and mixed audio/video only when neither is present. A single participant track must not silently fall back to mixed audio. Prior state/run metadata/transcripts and ambiguous inputs are held. New source claims have a locally persisted random owner token; old tokenless claims require reconciliation and cannot be silently adopted by the hardened version.

## Continuous-service safety

Use a new local `state_root` when changing engine identity. Configure `history_queues` with all previous durable consumer queues. They are read read-only; a missing/unreadable queue blocks discovery. Existing submissions, matching source hashes and existing NAS claims require reconciliation. This is not an exhaustive search of historical remote Hub content; unrecorded older processing may still need operator review.

Set `safe_continuous_intake: true` for unattended operation. In this mode **participant-track execution is disabled**, even if an identity mapping is supplied: timeline/coverage verification and production integration remain unfinished. Such bundles are reported with `participant_alignment_and_coverage_required` unless an older job/claim already blocks them. Never turn off this guard just to clear a held meeting. Zoom VTT must contain valid named cues; required Zoom turns cannot fall back to pyannote. Mixed audio is eligible only when participant tracks and VTT are absent.

The service scans every `scan_seconds` (60 in the current node configuration). `scan.json` lists candidates and held reasons, while `health.json` reports queue counts and runtime errors. Held scan entries are not newly enqueued jobs. On startup, full source hashing can delay the first health report. SIGTERM fences the worker's ownership checks and terminates its own ASR child group; partial results remain held rather than being silently rerun. Do not start the obsolete mixed-only service configuration.

Stages: verified local input → inherited receipted ASR with VoicePrints off → NAS archive. Completion moves the original meeting directory to `<heavy>/<project>/sessions/NAS-<job-id>/source` by same-filesystem rename only after verified result copies and receipts exist. Result files are under `transcripts`; `session.json` identifies the source and processing binding. `hub-delivery.json` is explicitly pending. NAS completion does not claim Hub publication or semantic acceptance.

`nas_hub_upload.py --session <archive>` validates the full source inventory, receipt bindings and result hashes offline. `--run --remote gdrive:Hub --rclone-config <private>` explicitly enables text-only Hub delivery; audio/video/chunks stay on NAS. Existing differing or foreign remote artifacts block publication. A delivery manifest is written last and read back before the local delivery flag changes. This helper is not connected to a timer or background retry loop and has not passed real Hub acceptance; it is tested with a synthetic Drive. It never calls ASR or removes source files. Do not install it into the active snapshot of an in-progress run.

Persistent source claims are never stolen; changed inputs or processing identity require reconciliation. Interrupted ASR is not automatically retried without proof; the existing runtime keeps inflight work recovery-required. Existing archive content is never overwritten on a checksum mismatch. Original/local retained files and disk reservations are preserved.

CLI:

```text
python src/nas_inbox.py --config /private/nas-inbox.json --scan-only
python src/nas_inbox.py --config /private/nas-inbox.json
```

The first command writes local inventory/queue/scan metadata, but performs no ASR, NAS claim, archive or source mutation. Use a local durable state root, not CIFS. Set `mountpoint`, `mount_source`, `heavy_root`, `inbox_root`, node/CLI paths, the existing common budget settings and `gpu_lock`. `approved_inputs` lists exact project/meeting relative paths. `settle_seconds` defaults to 300. `Gelya-Asr` is excluded. Installing/enabling a systemd unit is a separate explicit operator action.
