# Speaker recognition (diarization + voiceprints)

Two layers turn a recording into a named, speaker-attributed transcript:

1. **Diarization** — *who spoke when*. pyannote segments the audio into speaker
   turns and labels them `SPEAKER_00`, `SPEAKER_01`, … (anonymous, per file).
2. **Voiceprints** — *who that speaker is*. An ECAPA-TDNN embedding of each voice
   is matched against a per-project registry, so the same person gets the same
   name **across sessions**.

Diarization alone needs only `speaker_mode: diarize`. Putting names on speakers
needs voiceprints (`voiceprint_mode`).

> **Privacy:** a voice embedding is biometric personal data. The embeddings store
> stays **node-local and off the shared hub**, and is never committed. Only the
> project registry (names + light profile cards) lives on the hub. See
> [PRIVACY.md](../PRIVACY.md).

## Requirements

- `speaker_mode: diarize`
- A Hugging Face token (`HF_TOKEN`) — gated pyannote models
- `voiceprint_mode` set to `enroll` or `match` (not `off`)

## Configuration (`node.local.json`)

```jsonc
"outputs": {
  "voiceprints": {
    "project_projection": "{hub_root}/{pid}",   // registry location — project root
    "local_cache":        "{cache_root}/{pid}"   // node-local embeddings store dir
  }
},
"voiceprint_mode": "off"   // off | enroll | match
```

- **`project_projection`** — directory for the per-project speaker registry
  (`index.json` + `profiles/`). Set to `{hub_root}/{pid}` to keep voiceprints **at
  the project root**, so a project folder is self-contained.
- **`local_cache`** — node-local directory for the embeddings store
  (`voiceprints.json`); keep it off the shared hub (biometric data).

## Where the data lives

| Path | Contents | On the hub? |
|---|---|---|
| `{hub_root}/{pid}/index.json` | registry summary (names + profile index) | yes (shareable) |
| `{hub_root}/{pid}/profiles/{id}.json` | per-speaker card (name, best clip, meta) | yes |
| `{cache_root}/{pid}/voiceprints.json` | raw ECAPA embeddings (biometric) | **no — node-local only** |

## Modes

- **`off`** — diarization only; transcript keeps `SPEAKER_00/01…`.
- **`enroll`** — build the project registry from new voices (the first bootstrap pass).
- **`match`** — identify speakers in new sessions against the existing registry.

## First-time workflow

1. Set `speaker_mode: diarize`, `HF_TOKEN`, and `voiceprint_mode: enroll`.
2. Process the project's sessions. The registry is created at `{hub_root}/{pid}/`
   with one profile per distinct voice (still anonymous — keyed by `voice_hash`).
3. **Name the speakers:** open `{hub_root}/{pid}/profiles/{id}.json` and set
   `canonical_name` (and optionally a display name) for each person.
4. Switch to `voiceprint_mode: match`. Future sessions in that project now resolve
   those voices to their names automatically.

Re-running `enroll` is idempotent — new voices are appended, existing ones get
extra embeddings (improves matching); manually set names are not overwritten.

## How the watcher wires it

`run_asr` resolves `outputs.voiceprints.project_projection` and `local_cache` with
the file's `pid` and passes three flags to `media_transcribe_cli`:
`--project-speaker-registry`, `--voiceprint-store`, `--voiceprint-mode`. It is
gated on `voiceprint_mode != off` and a real project id — reserved pids (`_inbox`,
`_unrouted`, anything starting with `_`) are skipped so test/unsorted audio never
seeds a project registry.

## Direct CLI (one file, no watcher)

```bash
python src/media_transcribe_cli.py \
    --input recording.m4a --output-dir out --project-id 700 \
    --speaker-mode diarize \
    --project-speaker-registry "/path/hub/700" \
    --voiceprint-store "/path/cache/700/voiceprints.json" \
    --voiceprint-mode enroll
```

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Speakers stay `SPEAKER_00/01` | `voiceprint_mode: off`; or registry empty (run `enroll` first); or `canonical_name` not set on the profiles |
| `profile_store_path is required for voiceprint mode` | voiceprint mode is on but no store path — configure `outputs.voiceprints.local_cache` (the watcher then passes `--voiceprint-store` automatically) |
| No speaker turns at all | `speaker_mode` is not `diarize`, or `HF_TOKEN` missing/invalid |
| Registry not created for a project | the pid is reserved (`_…`) and intentionally skipped, or `voiceprint_mode` is `off` |
| Names don't carry to a new session | still in `enroll` (or `off`) — switch to `match` once names are set |

---

See also: [`deployment.md`](deployment.md) · `config/node.example.json`
(`outputs.voiceprints`, `voiceprint_mode`).
