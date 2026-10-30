# Admission-package NAS pilot

Version: 0.0.3 · Status: candidate, not a service deployment

## Read-only compatibility (no execution)

`nas_pilot.py --config /private/package-check.json --check-package` supports
admission formats `0.0.1` (one input) and `0.0.2` (two inputs with separate
`source-validation-initial.json` / `source-validation-philipp.json`). The config
needs only `project`, `corpus_root`, `mountpoint`, `mount_source`,
`package_relative`, and the exact `manifest_sha256`. It is not a service config
and must not replace a live config.

This path verifies the NAS identity, all manifest members and source hashes,
per-source validation bindings, exclusions, native timeline and off policy.
It reads adjacent terminal state/receipt/output inventories before considering
the current engine/profile. A verified completion returns `reuse_completed`
with its ORIGINAL job/manifest/receipt reference; a source without verified
adjacent NAS completion returns `execution_held`. This is NOT evidence that the
source was never processed by Hub, legacy or another node: the report explicitly
limits completion evidence to `adjacent-nas-only`, with external searches not
performed. Shared semantic acceptance is not assessed; even reuse carries a
`shared_semantic_acceptance_required` blocker, not a claim of accepted content.
Partial, mismatched or corrupt completion evidence is blocked,
never treated as a new candidate. Historical package fields are not rewritten.

Read-back checks the source-bound collection marker and the exact set of result
files (receipt plus inventory); unlisted artifacts are rejected. Empty receipted
files are allowed, consistent with the existing delivery contract. Scope/held
combinations are format-specific, and exclusions conservatively match NFC/case
aliases for shared Windows/CIFS corpora. Malformed documents raise EvidenceError.

Compatibility acceptance never authorizes execution or semantic acceptance.
The path does not construct a queue, worker, claim, result area, READY or service;
it reads the corpus and prints JSON only. The result includes integrity,
compatibility, source-preparation/disposition, completion refs and explicit
`execution_authorized=false`, `runtime_ready=false`. Missing or unpinned inputs
fail closed; no fallback to older packages.

`--preflight` and `--run-pilot` below deliberately remain limited to the original
single-input `0.0.1` execution contract. Supporting `0.0.2` for inspection does
not permit a second/batch run, remove execution-held, or modify the old run.

## Original bounded execution contract

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
