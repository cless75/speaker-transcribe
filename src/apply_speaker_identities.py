#!/usr/bin/env python
import datetime as dt
import json
import os
import pathlib
import sys


def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8", "surrogatepass"))


def save_json(path: pathlib.Path, payload: dict) -> None:
    path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8", "surrogatepass"))


def normalize_mapping(raw: dict | None) -> dict[str, str]:
    mapping = {}
    for key, value in (raw or {}).items():
        if key is None or value is None:
            continue
        src = str(key).strip()
        dst = str(value).strip()
        if src and dst:
            mapping[src] = dst
    return mapping


def normalize_id_list(raw: list | tuple | set | None) -> list[str]:
    values = []
    for item in raw or []:
        value = str(item).strip()
        if value:
            values.append(value)
    return values


def timestamp_hms(total_seconds: float) -> str:
    whole = max(0, int(float(total_seconds or 0)))
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    seconds = whole % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def timestamp_vtt(total_seconds: float) -> str:
    safe = max(0.0, float(total_seconds or 0))
    hours = int(safe // 3600)
    minutes = int((safe % 3600) // 60)
    seconds = int(safe % 60)
    milliseconds = int(round((safe - int(safe)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def save_text(path: pathlib.Path, content: str) -> None:
    path.write_bytes(content.encode("utf-8", "surrogatepass"))


def cyrillic_score(value: str) -> int:
    return sum(1 for char in value if ("А" <= char <= "я") or char in {"Ё", "ё"})


def repair_text(value: str) -> str:
    if not isinstance(value, str) or not value:
        return value
    candidates = [value]
    for codec in ("utf-8", "cp1251"):
        try:
            candidates.append(value.encode("latin1", "surrogateescape").decode(codec))
        except Exception:
            pass
    best = value
    best_score = cyrillic_score(value)
    for candidate in candidates[1:]:
        score = cyrillic_score(candidate)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def rebuild_md(raw: dict) -> str:
    frontmatter = "\n".join(
        [
            "---",
            f"source_file: {json.dumps(raw.get('source_file'))}",
            f"source_type: {json.dumps(raw.get('source_type'))}",
            f"language: {json.dumps(raw.get('language_detected'))}",
            f"engine: {json.dumps(raw.get('engine'))}",
            f"model: {json.dumps(raw.get('model'))}",
            f"quality_preset: {json.dumps(raw.get('quality_preset'))}",
            f"execution_profile: {json.dumps(raw.get('execution_profile'))}",
            f"duration: {json.dumps(raw.get('duration_sec'))}",
            f"created_at: {json.dumps(raw.get('created_at'))}",
            "---",
            "",
            "## Transcript",
            "",
        ]
    )
    transcript_lines = []
    for segment in raw.get("segments", []):
        speaker_label = repair_text(segment.get("speaker_name") or segment.get("speaker_id") or segment.get("speaker"))
        speaker_source = segment.get("speaker_source", "unknown")
        source_suffix = f" [{speaker_source}]" if speaker_label else ""
        speaker_prefix = f"{speaker_label}{source_suffix}: " if speaker_label else ""
        transcript_lines.append(
            f"[{timestamp_hms(segment['start'])}] {speaker_prefix}{repair_text(segment.get('text', ''))}".strip()
        )
    return frontmatter + "\n".join(transcript_lines) + "\n"


def rebuild_vtt(raw: dict) -> str:
    lines = ["WEBVTT", ""]
    for segment in raw.get("segments", []):
        speaker_label = repair_text(segment.get("speaker_name") or segment.get("speaker_id") or segment.get("speaker"))
        speaker_source = segment.get("speaker_source", "unknown")
        source_suffix = f" [{speaker_source}]" if speaker_label else ""
        speaker_prefix = f"{speaker_label}{source_suffix}: " if speaker_label else ""
        lines.append(f"{timestamp_vtt(segment['start'])} --> {timestamp_vtt(segment['end'])}")
        lines.append(f"{speaker_prefix}{repair_text(segment.get('text', ''))}".strip())
        lines.append("")
    return "\n".join(lines) + "\n"


def rebuild_txt(raw: dict) -> str:
    return "\n".join(repair_text(str(segment.get("text", ""))) for segment in raw.get("segments", [])) + "\n"


def ensure_profile(store: dict, voice_hash: str) -> dict:
    profiles = store.setdefault("profiles", [])
    existing = next((item for item in profiles if item.get("voice_hash") == voice_hash), None)
    if existing is not None:
        existing.setdefault("clip_history", [])
        return existing
    created = {
        "voice_hash": voice_hash,
        "display_name": None,
        "contact_ref": None,
        "contact_name": None,
        "embeddings": [],
        "updated_at": None,
        "best_clip_path": None,
        "best_clip_source_file": None,
        "best_clip_updated_at": None,
        "best_clip_score": None,
        "clip_history": [],
    }
    profiles.append(created)
    return created


def maybe_update_best_clip(profile: dict, clip: dict, source_file: str) -> None:
    intervals = clip.get("source_intervals") or []
    total = sum(max(0.0, float(item["end"]) - float(item["start"])) for item in intervals)
    longest = max((max(0.0, float(item["end"]) - float(item["start"])) for item in intervals), default=0.0)
    fragments = len(intervals)
    score = [round(longest, 6), round(total, 6), -int(fragments)]
    now = dt.datetime.now(dt.UTC).isoformat()
    profile.setdefault("clip_history", []).append(
        {
            "clip_path": clip.get("clip_path"),
            "source_file": source_file,
            "selection_method": clip.get("selection_method"),
            "intervals": intervals,
            "updated_at": now,
            "score": score,
        }
    )
    current = profile.get("best_clip_score")
    if current is None or tuple(score) > tuple(current):
        profile["best_clip_path"] = clip.get("clip_path")
        profile["best_clip_source_file"] = source_file
        profile["best_clip_updated_at"] = now
        profile["best_clip_score"] = score
        profile["updated_at"] = now


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "surrogatepass"))
    raw_path = pathlib.Path(payload["raw_path"])
    run_meta_path = pathlib.Path(payload["run_meta_path"])
    text_paths = [pathlib.Path(item) for item in payload.get("text_paths", [])]
    speaker_map = normalize_mapping(payload.get("speaker_map"))
    drop_speaker_ids = set(normalize_id_list(payload.get("drop_speaker_ids")))
    clear_voiceprint_speaker_ids = set(normalize_id_list(payload.get("clear_voiceprint_speaker_ids")))
    delete_clip_files = bool(payload.get("delete_clip_files"))
    voiceprint_bindings = payload.get("voiceprint_bindings") or {}
    profile_store_path = payload.get("profile_store_path")

    raw = load_json(raw_path)
    run_meta = load_json(run_meta_path)
    source_file = raw.get("source_file") or run_meta.get("source_file")

    if isinstance(raw.get("speaker_map"), dict):
        raw["speaker_map"] = {key: repair_text(value) for key, value in raw["speaker_map"].items()}
    if isinstance(run_meta.get("speaker_map"), dict):
        run_meta["speaker_map"] = {key: repair_text(value) for key, value in run_meta["speaker_map"].items()}

    filtered_segments = []
    for segment in raw.get("segments", []):
        if segment.get("speaker_name"):
            segment["speaker_name"] = repair_text(segment["speaker_name"])
        if segment.get("speaker"):
            segment["speaker"] = repair_text(segment["speaker"])
        if segment.get("text"):
            segment["text"] = repair_text(segment["text"])
        speaker_id = segment.get("speaker_id")
        if speaker_id in drop_speaker_ids:
            continue
        if speaker_id in clear_voiceprint_speaker_ids:
            segment["voice_hash"] = None
            if speaker_id not in speaker_map:
                segment["speaker_name"] = speaker_id
                segment["speaker"] = speaker_id
                segment["speaker_source"] = "unknown"
        if speaker_id and speaker_id in speaker_map:
            segment["speaker_name"] = speaker_map[speaker_id]
            segment["speaker"] = speaker_map[speaker_id]
            segment["speaker_source"] = "manual_map"
        if speaker_id and speaker_id in voiceprint_bindings:
            binding = voiceprint_bindings[speaker_id] or {}
            if binding.get("contact_name") and segment.get("speaker_source") != "manual_map":
                segment["speaker_name"] = binding["contact_name"]
                segment["speaker"] = binding["contact_name"]
                segment["speaker_source"] = "voiceprint_contact"
            if binding.get("voice_hash"):
                segment["voice_hash"] = binding["voice_hash"]
        filtered_segments.append(segment)

    raw["segments"] = filtered_segments

    merged_speaker_map = {**(raw.get("speaker_map") or {}), **speaker_map}
    raw["speaker_map"] = {
        speaker_id: speaker_name
        for speaker_id, speaker_name in merged_speaker_map.items()
        if speaker_id not in drop_speaker_ids
    }
    run_meta["speaker_map"] = dict(raw["speaker_map"])

    filtered_clips = []
    for clip in raw.get("speaker_clips", []):
        if clip.get("speaker_name"):
            clip["speaker_name"] = repair_text(clip["speaker_name"])
        speaker_id = clip.get("speaker_id")
        if speaker_id in drop_speaker_ids:
            if delete_clip_files and clip.get("clip_path"):
                try:
                    pathlib.Path(clip["clip_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            continue
        if speaker_id in clear_voiceprint_speaker_ids:
            clip["profile_id"] = None
            clip["profile_link_status"] = "unlinked"
            clip["profile_clip_path"] = None
            if speaker_id not in speaker_map:
                clip["speaker_name"] = speaker_id
                clip["speaker_source"] = "unknown"
        if speaker_id in speaker_map:
            clip["speaker_name"] = speaker_map[speaker_id]
            clip["speaker_source"] = "manual_map"
        binding = voiceprint_bindings.get(speaker_id) or {}
        if binding.get("voice_hash"):
            clip["profile_id"] = binding["voice_hash"]
            clip["profile_link_status"] = "linked"
        if binding.get("contact_name") and clip.get("speaker_source") != "manual_map":
            clip["speaker_name"] = binding["contact_name"]
            clip["speaker_source"] = "voiceprint_contact"
        filtered_clips.append(clip)

    raw["speaker_clips"] = filtered_clips

    run_meta["speaker_clips"] = raw.get("speaker_clips", [])

    if profile_store_path:
        store_path = pathlib.Path(profile_store_path)
        store = load_json(store_path) if store_path.exists() else {"schema_version": "v2", "profiles": []}
        for clip in raw.get("speaker_clips", []):
            profile_id = clip.get("profile_id")
            if not profile_id or not clip.get("clip_path"):
                continue
            profile = ensure_profile(store, profile_id)
            if clip.get("speaker_name"):
                profile["display_name"] = clip["speaker_name"]
                profile["contact_name"] = clip["speaker_name"]
            maybe_update_best_clip(profile, clip, source_file)
            clip["profile_clip_path"] = profile.get("best_clip_path")
        save_json(store_path, store)

    save_json(raw_path, raw)
    save_json(run_meta_path, run_meta)

    for text_path in text_paths:
        if not text_path.exists():
            continue
        suffix = text_path.suffix.lower()
        name = text_path.name.lower()
        if suffix == ".md":
            save_text(text_path, rebuild_md(raw))
        elif suffix == ".vtt":
            save_text(text_path, rebuild_vtt(raw))
        elif suffix == ".txt" or "transcript" in name:
            save_text(text_path, rebuild_txt(raw))

    print(
        json.dumps(
            {
                "status": "ok",
                "raw_path": str(raw_path),
                "run_meta_path": str(run_meta_path),
                "text_paths": [str(item) for item in text_paths],
                "speaker_map": run_meta.get("speaker_map", {}),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
