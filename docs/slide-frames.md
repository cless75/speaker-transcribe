# Slide frames: screenshots + local OCR

An opt-in stage that turns a video into an illustrated transcript: frames captured at
key moments, the text on them read by a local OCR engine, both interleaved into the
`.md` by timecode. Everything runs locally — nothing leaves the machine.

Enable with `video_frames.mode` in the node config, or `--slide-frames` on the CLI.
Default is `off`: the engine ships the mechanism, the consumer sets the policy.

## Which video the frames come from

A meeting bundle ships several videos of the same session. The one picked for ASR is
the **worst** for frames: the watcher prefers the small active-speaker track because
it decodes fast, while the shared screen lives in the heavy file.

```jsonc
"video_frames": {
  "source": {
    "prefer_patterns": ["_as_", "_\\d{3,}x\\d{3,}"],  // regexes over the file name, in order
    "exclude_patterns": ["_avo_"],                    // never take frames from these
    "fallback": "largest"                             // or omit: keep using the ASR input
  }
}
```

On the CLI the same thing is `--frames-input <path>`. Without a rule the stage keeps
using the ASR input, exactly as before.

**Why this matters.** On one real session the frames were captured from the
speaker-only track: 139 screenshots, not one of them carrying slide text — only the
name plate of whoever was talking. The recording of the shared screen sat next to it,
untouched. The defect survived a month because nothing recorded *which file* the
frames came from; `slides.json` now always carries `source_path`.

## Which moments get captured

| Mode | How | Good for |
|---|---|---|
| `slide-change` | ffmpeg scene detection (`scene_threshold`, `min_gap_sec`) | Screen-only recordings, screencasts |
| `interval` | every `interval_sec` seconds | Meeting recordings with a "speaker + shared screen" layout |

Scene detection sounds like the obvious choice and fails on meeting recordings: the
slide occupies part of a mostly static frame, so changing it does not move the scene
score. Measured on a 2:32 session — threshold 0.4 gave 29 frames, 0.15 gave 32, and
**between 00:05 and 01:13 neither produced a single frame**, which is exactly when the
screen was being shared. Interval capture plus the filter below is the reliable path.

## Which frames reach the transcript

Interval capture buys coverage at the cost of noise: a talking head carries no slide
text, and one slide held for an hour yields sixty near-identical frames.

```jsonc
"filter": {
  "min_ocr_chars": 90,       // shorter OCR text -> not a slide
  "dedupe_similarity": 0.85, // 0..1 over OCR character n-grams
  "dedupe_visual": 0.95      // 0..1 over a 64-bit difference hash of the picture
}
```

`dedupe_visual` is the one that works. OCR reads the same slide differently on every
pass — "To make sure your strategy" comes back as "To make sure your strar" — so the
text drifts while the picture does not. On that same 2:32 session: text dedupe alone
kept **101 frames of 152**; adding `dedupe_visual: 0.95` brought it to **27**, and the
diagram that had been on screen for an hour collapsed from sixty frames to five.

The hashes cost about 30 seconds per recording (one ffmpeg call per frame, CPU).

**Nothing is discarded silently.** Filtered frames stay on disk and in `slides.json`
with `embed: false` and a `filter_reason` (`no_text` or `duplicate`, plus
`duplicate_of: text | picture`). Counts land in `slides.filter`. A frame that vanishes
without a trace is indistinguishable from a bug.

## Output

| Path | Contents |
|---|---|
| `<output_dir>/frames/*.png` | every captured frame, including the filtered ones |
| `<output_dir>/<base>-slides.json` | timecodes, OCR text, `source_path`, filter verdicts |
| `<base>-transcript.md` | frames with `embed: true`, interleaved by timecode |

## Degradation

No ffmpeg → stage skipped. No RapidOCR → frames captured without text. A broken frame
→ logged, the rest continue. The ASR run is never broken by this stage.

---

See also: [`speaker-recognition.md`](speaker-recognition.md) · `config/node.example.json`
(`video_frames`).
