# Offline NAS Inbox

`nas_inbox.py` consumes `<heavy>/_inbox/<project>/<meeting>/` using the existing stage runtime, shared GPU lock and shared byte ledger. It never calls Drive. Initial deployment is an explicit approved-input batch; `continuous: true` rescans only that allowlist, not arbitrary historical media.

The scanner inventories and SHA-256 hashes all original files, rejects symlinks/normalization collisions and changing/recent inputs, and selects exactly one root-level mixed audio (one root-level video only as fallback). `Audio Record` participant tracks are preserved, not processed individually. Prior state/run metadata/transcripts and ambiguous inputs are held. Cross-Hub completion evidence still requires operator inspection before initial approval; filename/absence of a local marker is not proof that ASR never ran.

Stages: verified local input → inherited receipted ASR with VoicePrints off → NAS archive. Completion moves the original meeting directory to `<heavy>/<project>/sessions/NAS-<job-id>/source` by same-filesystem rename only after verified result copies and receipts exist. Result files are under `transcripts`; `session.json` identifies the source and processing binding. `hub-delivery.json` is explicitly pending; **the deferred Hub uploader is not implemented yet**. NAS completion does not claim Hub publication or semantic acceptance.

Persistent source claims are never stolen; changed inputs or processing identity require reconciliation. Interrupted ASR is not automatically retried without proof; the existing runtime keeps inflight work recovery-required. Existing archive content is never overwritten on a checksum mismatch. Original/local retained files and disk reservations are preserved.

CLI:

```text
python src/nas_inbox.py --config /private/nas-inbox.json --scan-only
python src/nas_inbox.py --config /private/nas-inbox.json
```

The first command writes local inventory/queue/scan metadata, but performs no ASR, NAS claim, archive or source mutation. Use a local durable state root, not CIFS. Set `mountpoint`, `mount_source`, `heavy_root`, `inbox_root`, node/CLI paths, the existing common budget settings and `gpu_lock`. `approved_inputs` lists exact project/meeting relative paths. `settle_seconds` defaults to 300. `Gelya-Asr` is excluded. Installing/enabling a systemd unit is a separate explicit operator action.
