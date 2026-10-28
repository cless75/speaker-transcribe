# Admission-package NAS pilot

Version: 0.0.1 · Status: candidate, not a service deployment

`nas_pilot.py` adds an explicit bounded E2E route from an owner-approved, checksum-pinned
admission package to the existing stage queue/receipts/engine and Hub+NAS sinks.
It does not turn the Hub adapter into a filesystem adapter or widen live scopes.

- Discovery consumes exactly one admitted `include` from a pinned manifest;
  verifies every listed package member, source length/SHA-256 and native timeline.
  No recursive corpus scanning or automatic extra video selection.
- Only unknown/recording-local speaker labels: `voiceprint_mode=off`, no matching,
  enrollment, global promotion, VTT/Ktalk sidecars, speaker name maps or glossary.
  Actual CLI payload defaults and returned voiceprint/run metadata are tested.
- The admission document is not a runtime config. Caller separately pins private
  node paths, NAS identity, budget/GPU resources, and inspected no-prior-completion
  evidence. Preflight ACK of inputs is not execution or release acceptance.
- Uses a separate durable local queue/state root, the SAME existing shared byte
  ledger/volume/config and GPU lock as Hub consumers. Scratch stays under the job
  directory and participates in byte accounting. Retained files are not deleted.
- A persistent source-bound NAS claim is never stolen/expired. Foreign/partial
  claims or a different existing processing identity require reconciliation.
  An unreceipted prior ASR invocation is held, not automatically rerun.
- The same `HubPipeline.asr` engine/completion/snapshot implementation is reused
  through policy hooks; the legacy Hub behavior remains the default.
- Both sinks must read back before terminal source state is written LAST, under
  the adjacent `<source>.asr` root. Source bytes and the owner package stay intact.
  Hub outputs are project-scoped, versioned by processing ID. All pilot outputs
  remain `needs_review` pending shared owner acceptance.
- Successful pilot re-verifies both deliveries and repeats package discovery
  twice; a completed source is reused, not enqueued as a new ASR.

Operator actions (private paths, never checked-in credentials):

```sh
python src/nas_pilot.py --config /private/nas-pilot.local.json --preflight
# After exact input ACK, local/remote duplicate checks and candidate regressions:
python src/nas_pilot.py --config /private/nas-pilot.local.json --run-pilot
```

`--run-pilot` is a single explicitly authorized E2E, not unattended serving and
not a workaround for `hub_pipeline.py` attestation. It never edits/installs units,
the active immutable release, live Hub config/queue, owner READY or old receipts.
SIGINT/SIGTERM request draining; the controlled engine is not killed by a polling
deadline. Full recovery/migration and arbitrary NAS batch serving are not claimed.
