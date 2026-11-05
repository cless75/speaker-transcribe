#!/usr/bin/env python3
"""Offline NAS Inbox consumer. Never calls Drive; Hub delivery is explicitly pending.

One project/meeting directory is one submission. Use mixed audio only (video
fallback), never treat participant tracks as independent sessions. Existing ASR
evidence or ambiguous compositions are held for operator reconciliation.
"""
from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
import uuid
from types import SimpleNamespace

from hub_pipeline import implementation
from nas_pilot import pilot_profile
from pipeline_budget import DiskBudget
import hub_worker as hub
from merge_speaker_tracks import merge_tracks
from pipeline_hub_adapter import HubPipeline, duration_seconds
from pipeline_process import AsrProcess, gpu_idle
from pipeline_queue import PipelineQueue
from pipeline_runtime import HoldJob, StageRunner
from pipeline_storage import (EvidenceError, ReceiptStore, atomic_json, canonical,
                              digest, file_lock, no_links, sync_directory)

AUDIO = {'.m4a', '.mp3', '.wav', '.ogg', '.oga'}
VIDEO = {'.mp4', '.webm', '.mov', '.mkv', '.avi', '.m4v'}
PID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\Z')


def inventory(root):
    """Full immutable input snapshot, including sidecars and ignored audio tracks."""
    root = no_links(pathlib.Path(root))
    found, names = [], set()
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = no_links(pathlib.Path(folder) / name)
            rel = path.relative_to(root).as_posix()
            normalized = unicodedata.normalize('NFC', rel).casefold()
            if normalized in names:
                raise HoldJob('ambiguous_input_names')
            names.add(normalized)
            if not path.is_file():
                continue
            before = path.stat()
            checksum = digest(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise HoldJob('input_changing')
            found.append({'path': rel, 'size': after.st_size, 'sha256': checksum})
    return sorted(found, key=lambda x: x['path'])


def select_primary(files):
    for item in files:
        name = item['path'].casefold()
        if (name.endswith(('-run-meta.json', '.state.json', '.processed-asr')) or
                any(p in {'transcripts', 'pipeline', '.801'} or p.endswith('.asr') for p in name.split('/'))):
            raise HoldJob('prior_processing_requires_reconciliation')
        if name.endswith(('.tmp', '.part', '.download')):
            raise HoldJob('incomplete_input')
    direct = [x['path'] for x in files if '/' not in x['path']]
    audio = [x for x in direct if pathlib.PurePosixPath(x).suffix.lower() in AUDIO]
    video = [x for x in direct if pathlib.PurePosixPath(x).suffix.lower() in VIDEO]
    if len(audio) == 1:
        return audio[0]
    if not audio and len(video) == 1:
        return video[0]
    raise HoldJob('ambiguous_or_missing_primary')


def select_input_plan(files):
    """Prefer named Zoom turns, then participant tracks, then mixed audio."""
    primary = select_primary(files)
    vtts = [x['path'] for x in files if pathlib.PurePosixPath(x['path']).suffix.lower() == '.vtt']
    tracks = [x['path'] for x in files if pathlib.PurePosixPath(x['path']).parts[:1] == ('Audio Record',)
              and pathlib.PurePosixPath(x['path']).suffix.lower() in AUDIO]
    if len(vtts) == 1:
        return {'mode': 'zoom_vtt', 'primary_name': primary, 'sidecars': vtts, 'tracks': []}
    if len(vtts) > 1:
        raise HoldJob('ambiguous_zoom_vtt')
    # A single participant file is not a meeting-level multi-track export.
    if len(tracks) >= 2:
        return {'mode': 'participant_tracks', 'primary_name': primary, 'sidecars': [], 'tracks': sorted(tracks)}
    return {'mode': 'mixed_diarization', 'primary_name': primary, 'sidecars': [], 'tracks': []}


def track_identity(path, identities):
    """Explicit identity only: a filename is never proof of a person identity."""
    value = (identities or {}).get(path)
    if not isinstance(value, dict) or not isinstance(value.get('display_name'), str) or not value['display_name'].strip():
        raise HoldJob('participant_track_identity_required')
    if value.get('voiceprint_consent') is not True:
        raise HoldJob('participant_voiceprint_consent_required')
    return value


def copy_verified(source, target, expected, check):
    target = no_links(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != expected['size'] or digest(target) != expected['sha256']:
            raise HoldJob('existing_copy_conflict')
        return
    temp = target.with_name('.copy-' + target.name)
    if temp.exists():
        raise HoldJob('partial_copy_requires_reconciliation')
    with source.open('rb') as inp, temp.open('xb') as out:
        for block in iter(lambda: inp.read(1024 * 1024), b''):
            check()
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    if temp.stat().st_size != expected['size'] or digest(temp) != expected['sha256']:
        raise HoldJob('copy_checksum_mismatch')
    os.replace(temp, target)
    sync_directory(target.parent)


class NasInboxPipeline(HubPipeline):
    def __init__(self, config, store, node, config_path, *, executor=None, probe=duration_seconds, gpu_check=gpu_idle):
        self.config, self.store, self.cfg, self.config_path = config, store, node, config_path
        self.executor, self.probe, self.gpu_check = executor, probe, gpu_check
        self.maximum_job = config['maximum_job']
        self.claims = self  # only job/resource+GPU locks; persistent NAS source claim below
        self.profile = hashlib.sha256(canonical(node)).hexdigest()
        self.engine = implementation()

    def guard(self):
        mount = pathlib.Path(self.config['mountpoint'])
        if self.config.get('test_local_filesystem'):
            return
        r = subprocess.run(['findmnt', '--json', '--target', str(mount), '--output', 'TARGET,SOURCE,FSTYPE'],
                           capture_output=True, text=True, timeout=15, check=True)
        entries = [x for x in json.loads(r.stdout)['filesystems'] if x['fstype'] != 'autofs']
        if len(entries) != 1 or entries[0]['target'] != str(mount) or entries[0]['source'] != self.config['mount_source'] or entries[0]['fstype'] != 'cifs':
            raise HoldJob('nas_mount_identity_changed')
        if shutil.disk_usage(mount).free < self.config['nas_minimum_free']:
            raise HoldJob('nas_disk_pressure')

    def source(self, job):
        data = json.loads(job['payload'])
        if data.get('adapter') != 'nas-inbox-v1' or data.get('delivery_policy') != 'nas-only':
            raise EvidenceError('invalid_nas_inbox_job')
        project, relative = data['project'], pathlib.PurePosixPath(data['relative'])
        if not PID.fullmatch(project) or project == 'Gelya-Asr' or len(relative.parts) != 2 or relative.parts[0] != project or any(p in {'.', '..'} for p in relative.parts):
            raise EvidenceError('invalid_nas_inbox_locator')
        expected = [data['submission'], data['revision'], self.profile, self.engine]
        if json.loads(job['identity']) != expected:
            raise EvidenceError('nas_inbox_identity_changed')
        primary = data['primary_name']
        if pathlib.PurePosixPath(primary).is_absolute() or '..' in pathlib.PurePosixPath(primary).parts:
            raise EvidenceError('invalid_primary_name')
        item = next(x for x in data['files'] if x['path'] == primary)
        return data, SimpleNamespace(project=project, path=primary, size=item['size'])

    def source_root(self, data):
        return no_links(pathlib.Path(self.config['inbox_root']) / data['relative'])

    def archive(self, job):
        data, item = self.source(job)
        return no_links(pathlib.Path(self.config['heavy_root']) / item.project / 'sessions' / ('NAS-' + job['id']))

    def route(self, job):
        _, item = self.source(job)
        return {'session_id': 'NAS-' + job['id'], 'target': item.project + '/sessions/NAS-' + job['id']}

    def public_source(self, job):
        data, _ = self.source(job)
        return self.archive(job) / 'source' / data['primary_name']

    def check(self, job):
        self.guard()
        data, _ = self.source(job)
        path = pathlib.Path(self.config['heavy_root']) / '_801-claims' / (hashlib.sha256(data['submission'].encode()).hexdigest() + '.json')
        local = no_links(self.store.directory(job['id']) / 'source-ownership.json')
        if (not local.is_file() or not path.is_file() or
                json.loads(path.read_bytes()) != json.loads(local.read_bytes()) or
                json.loads(local.read_bytes()).get('binding') != self.store.binding(job)):
            raise HoldJob('nas_source_claim_changed')

    def acquire(self, job):
        self.guard()
        data, _ = self.source(job)
        folder = pathlib.Path(self.config['heavy_root']) / '_801-claims'
        no_links(folder).mkdir(exist_ok=True)
        path = folder / (hashlib.sha256(data['submission'].encode()).hexdigest() + '.json')
        local = no_links(self.store.directory(job['id']) / 'source-ownership.json')
        if not local.exists():
            atomic_json(local, {'binding': self.store.binding(job), 'token': uuid.uuid4().hex,
                               'state_root': str(self.store.root)})
        ownership = json.loads(local.read_bytes())
        if not path.exists():
            with path.open('x', encoding='utf8') as out:
                json.dump(ownership, out)
                out.flush()
                os.fsync(out.fileno())
        self.check(job)

    def result_review(self, job, metadata):
        return 'needs_review'

    def worker_config(self, job, directory):
        cfg = dict(self.cfg)
        cfg['work_root'] = str(directory / 'work' / 'engine')
        cfg['session_artifact_dir'] = str(directory / 'work' / 'transcripts')
        data, _ = self.source(job)
        if data.get('input_mode') == 'zoom_vtt':
            cfg['zoom_vtt_path'] = str(directory / 'input' / data['sidecars'][0])
            # The worker's Zoom branch uses imported turns and does not invoke
            # pyannote even though this mode enables speaker assignment.
            cfg['speaker_mode'] = 'diarize'
        path = directory / 'worker-snapshot.json'
        if path.exists() and json.loads(path.read_bytes()) != cfg:
            raise HoldJob('worker_snapshot_changed')
        atomic_json(path, cfg)
        return cfg, path

    def _track_config(self, job, directory, track, identity):
        cfg, _ = self.worker_config(job, directory)
        cfg = copy.deepcopy(cfg)
        cfg.update(speaker_mode='off', voiceprint_mode='enroll',
                   voiceprint_enroll_name=identity['display_name'],
                   machine_local_voiceprint_store_path=str(directory / 'work' / 'Speakers' / 'voiceprints.json'),
                   session_artifact_dir=str(directory / 'work' / 'transcripts' / 'tracks' / hashlib.sha256(track.encode()).hexdigest()[:16]))
        path = directory / 'work' / 'track-configs' / (hashlib.sha256(track.encode()).hexdigest()[:16] + '.json')
        atomic_json(path, cfg)
        return cfg, path

    def validate_engine_result(self, job, metadata, output):
        vp = metadata.get('voiceprint') or {}
        if vp.get('mode') != 'off' or vp.get('enabled') is not False or metadata.get('profile_updates'):
            raise HoldJob('unexpected_voiceprint_updates')

    def asr(self, job, directory, check):
        """Run per-track ASR and merge it; mixed/Zoom retain the inherited worker."""
        data, _ = self.source(job)
        if data.get('input_mode') != 'participant_tracks':
            return super().asr(job, directory, check)
        self.check(job); check(); self.gpu_check()
        completion = directory / 'engine-completion.json'
        if completion.exists():
            value = json.loads(completion.read_bytes())
            if value.get('binding') != self.store.binding(job):
                raise EvidenceError('engine_completion_changed')
            return self.snapshot_result(job, directory, value, check)
        output = no_links(directory / 'work' / 'transcripts')
        output.mkdir(parents=True, exist_ok=True)
        tracks, profiles = [], []
        for track in data['tracks']:
            check(); self.check(job)
            identity = track_identity(track, self.config.get('track_identities'))
            local = directory / 'input' / track
            target = output / 'tracks' / hashlib.sha256(track.encode()).hexdigest()[:16]
            target.mkdir(parents=True, exist_ok=True)
            cfg, config_path = self._track_config(job, directory, track, identity)
            if self.executor is None:
                control = AsrProcess(directory, check=check, ownership_check=lambda: self.check(job))
                with (directory / 'private-asr.log').open('a', encoding='utf8') as log, contextlib.redirect_stderr(log):
                    ok, _, transcript, meta = hub.watcher.run_asr(local, cfg, data['project'], local.parent, target, config_path, process_control=control)
                if not ok: raise HoldJob('track_asr_did_not_complete')
            else:
                transcript, meta = self.executor(local, target, cfg, config_path, data['project'])
            if transcript is None or not transcript.is_file() or transcript.stat().st_size == 0:
                raise HoldJob('incomplete_track_asr_result')
            segments = next(iter(target.glob('*-segments.jsonl')), None)
            if segments is None:
                raise HoldJob('track_segments_missing')
            tracks.append((identity['display_name'], segments))
            profiles.append({'track': track, 'display_name': identity['display_name'], 'voiceprint': meta.get('voiceprint', {}) if isinstance(meta, dict) else {}})
        merged = merge_tracks(tracks, output, 'session', title='Session transcript')
        (output / 'Speakers').mkdir(exist_ok=True)
        store = directory / 'work' / 'Speakers' / 'voiceprints.json'
        if not store.is_file() or store.stat().st_size == 0:
            raise HoldJob('voiceprint_enrollment_missing')
        shutil.copyfile(store, output / 'Speakers' / 'voiceprints.json')
        atomic_json(output / 'Speakers' / 'voiceprint-enrollment.json', {'mode': 'participant_tracks', 'profiles': profiles})
        run_meta = pathlib.Path(merged['run_meta'])
        # The common receipt path requires a result manifest; the merger is its own engine.
        atomic_json(output / 'session-run-meta.json', {'status': 'ok', 'mode': 'participant_tracks',
                    'diarization': {'enabled': False, 'reason': 'participant_tracks'}, 'voiceprint': {'enabled': True, 'mode': 'enroll'},
                    'outputs': {'md': merged['outputs']['transcript_md'], 'vtt': merged['outputs']['segments_vtt'],
                                'segments_jsonl': merged['outputs']['merged_segments_jsonl'], 'run_meta': str(run_meta)}})
        inventory = self.store.inventory(job['id'], 'work/transcripts')
        value = {'schema': 1, 'binding': self.store.binding(job), 'files': inventory,
                 'metadata': {'quality': 'ok', 'review': 'needs_review', 'transcript_quality': {},
                              'transcript': 'transcripts/' + pathlib.Path(merged['outputs']['transcript_md']).name, **self.route(job)}}
        atomic_json(completion, value)
        return self.snapshot_result(job, directory, value, check)

    def stage(self, job, directory, check):
        self.acquire(job)
        data, _ = self.source(job)
        root = self.source_root(data)
        if inventory(root) != data['files']:
            raise HoldJob('nas_input_changed')
        selected = [data['primary_name'], *data.get('sidecars', []), *data.get('tracks', [])]
        selected = list(dict.fromkeys(selected))
        chosen = [next(x for x in data['files'] if x['path'] == path) for path in selected]
        for item in chosen:
            copy_verified(root / item['path'], directory / 'input' / item['path'], item, check)
        durations = [self.probe(directory / 'input' / item['path']) for item in chosen
                     if pathlib.PurePosixPath(item['path']).suffix.lower() in AUDIO | VIDEO]
        duration = max(durations, default=0)
        estimate = sum(2 * item['size'] for item in chosen) + math.ceil(duration * 256000) + math.ceil(duration / 60) * 8 * 1024**2 + 1024**3
        if estimate > self.maximum_job:
            raise HoldJob('media_expansion_exceeds_job_budget')
        if inventory(root) != data['files']:
            raise HoldJob('nas_input_changed')
        return {'source_verified': True, 'estimated_bytes': estimate, 'duration_seconds': duration}

    def publish(self, job, directory, check):
        self.check(job)
        data, _ = self.source(job)
        archive = self.archive(job)
        binding = archive / 'archive-binding.json'
        if archive.exists():
            if not binding.is_file() or json.loads(binding.read_bytes()) != self.store.binding(job):
                raise HoldJob('archive_not_owned')
        else:
            archive.mkdir(parents=True, exist_ok=False)
            # Persist ownership before any copies; an interrupted/unbound folder
            # requires reconciliation, never an automatic adoption.
            atomic_json(binding, self.store.binding(job))
        receipt = self.store.read(job, 'ready')
        if receipt is None:
            raise HoldJob('missing_asr_receipt')
        source = self.source_root(data)
        archived_source = archive / 'source'
        if source.exists():
            if archived_source.exists():
                raise HoldJob('archive_source_collision')
            if inventory(source) != data['files']:
                raise HoldJob('nas_input_changed_before_archive')
        elif not archived_source.exists() or inventory(archived_source) != data['files']:
            raise HoldJob('source_missing_requires_reconciliation')
        for member in receipt['artifacts']:
            relative = pathlib.PurePosixPath(member['path']).relative_to('result')
            copy_verified(directory / member['path'], archive / relative, member, check)
        for stage in ('discovered', 'ready'):
            src = self.store.path(job['id'], stage)
            copy_verified(src, archive / 'receipts' / src.name,
                          {'size': src.stat().st_size, 'sha256': digest(src)}, check)
        atomic_json(archive / 'hub-delivery.json', {'status': 'pending', 'reason': 'offline_nas_processing', 'job_id': job['id']})
        if source.exists():
            # Same mounted filesystem; never copy/delete the last source copy.
            os.rename(source, archived_source)
        if inventory(archived_source) != data['files']:
            raise HoldJob('archived_source_verification_failed')
        atomic_json(archive / 'session.json', {'status': 'asr-done', 'binding': self.store.binding(job),
                    'source_files': data['files'], 'result_files': receipt['artifacts'], 'hub_delivery': 'pending', 'review': 'needs_review'})
        return {'deliveries': {'nas': {'verified': True, 'evidence': {'archive': str(archive)}}}}

    def verify_completed(self, job, receipt, check):
        self.check(job)
        data, _ = self.source(job)
        archive = self.archive(job)
        if json.loads((archive / 'archive-binding.json').read_bytes()) != self.store.binding(job):
            raise HoldJob('archive_not_owned')
        state = json.loads((archive / 'session.json').read_bytes())
        if state['binding'] != self.store.binding(job) or inventory(archive / 'source') != data['files']:
            raise HoldJob('archive_identity_changed')
        ready = self.store.read(job, 'ready')
        for member in ready['artifacts']:
            path = archive / pathlib.PurePosixPath(member['path']).relative_to('result')
            check()
            if path.stat().st_size != member['size'] or digest(path) != member['sha256']:
                raise HoldJob('archived_result_changed')
        return True

    def discover(self, queue):
        self.guard()
        report = {'candidates': 0, 'held': []}
        inbox = no_links(pathlib.Path(self.config['inbox_root']))
        for project in sorted(inbox.iterdir()):
            if not project.is_dir() or not PID.fullmatch(project.name) or project.name == 'Gelya-Asr':
                continue
            for folder in sorted(project.iterdir()):
                if not folder.is_dir() or folder.name.startswith('.'):
                    continue
                relative = folder.relative_to(inbox).as_posix()
                if self.config.get('approved_inputs') is not None and relative not in self.config['approved_inputs']:
                    continue
                try:
                    files = inventory(folder)
                    if not files or any(time.time() - p.stat().st_mtime < self.config.get('settle_seconds', 300)
                                        for p in folder.rglob('*') if p.is_file()):
                        raise HoldJob('input_not_stable')
                    plan = select_input_plan(files)
                    primary = plan['primary_name']
                    checksum = next(x['sha256'] for x in files if x['path'] == primary)
                    revision = hashlib.sha256(canonical(files)).hexdigest()
                    submission = 'nas-inbox:' + relative
                    old = [dict(r) for r in queue.db.execute('select * from pipeline_jobs')
                           if json.loads(r['payload']).get('submission') == submission]
                    if old:
                        if any(json.loads(x['payload'])['revision'] != revision for x in old):
                            raise HoldJob('previous_submission_changed')
                        continue
                    for previous in queue.db.execute('select payload from pipeline_jobs'):
                        old_data = json.loads(previous['payload'])
                        old_primary = old_data.get('primary_name')
                        if any(x['path'] == old_primary and x['sha256'] == checksum for x in old_data.get('files', [])):
                            raise HoldJob('duplicate_primary_requires_reconciliation')
                    data = {'adapter': 'nas-inbox-v1', 'delivery_policy': 'nas-only', 'project': project.name,
                            'relative': relative, 'primary_name': primary, 'input_mode': plan['mode'],
                            'sidecars': plan['sidecars'], 'tracks': plan['tracks'], 'files': files,
                            'submission': submission, 'revision': revision}
                    queue.enqueue(submission, revision, self.profile, self.engine, data)
                    report['candidates'] += 1
                except (HoldJob, OSError, EvidenceError) as exc:
                    report['held'].append({'relative': relative, 'reason': str(exc)})
        return report


def run(config, *, scan_only=False):
    root = no_links(pathlib.Path(config['state_root']))
    root.mkdir(parents=True, exist_ok=True)
    if root.is_relative_to(pathlib.Path(config['mountpoint'])):
        raise ValueError('local_state_required')
    heavy = no_links(pathlib.Path(config['heavy_root']))
    if no_links(pathlib.Path(config['inbox_root'])) != heavy / '_inbox':
        raise ValueError('explicit_heavy_inbox_required')
    node = pilot_profile(json.loads(pathlib.Path(config['node_config']).read_bytes()))
    node['transcribe_cli'] = config['transcribe_cli']
    node['_config_dir'] = str(pathlib.Path(config['node_config']).parent)
    # The reused engine writes public path refs to NAS, never to Hub in this mode.
    node['hub_root'] = config['heavy_root']
    snapshot = root / 'node-snapshot.json'
    if snapshot.exists() and json.loads(snapshot.read_bytes()) != node:
        raise ValueError('node_snapshot_drift')
    atomic_json(snapshot, node)
    store = ReceiptStore(root)
    pipeline = NasInboxPipeline(config, store, node, snapshot)
    with file_lock(root / 'supervisor.lock'):
        queue = PipelineQueue(root / 'queue.sqlite3')
        try:
            report = pipeline.discover(queue)
            atomic_json(root / 'scan.json', report)
            if scan_only:
                return report
        finally:
            queue.close()
        stop, errors = threading.Event(), []
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *args: stop.set())
        class Publisher:
            def __call__(self, *args): return pipeline.publish(*args)
            def verify_completed(self, *args): return pipeline.verify_completed(*args)
        def consume(stage):
            budget = runner = None
            try:
                budget = DiskBudget(pathlib.Path(config['budget_path']), capacity=config['buffer_capacity'],
                    minimum_free=config['local_minimum_free'], maximum_job=config['maximum_job'], volume=pathlib.Path(config['budget_volume']))
                runner = StageRunner(root/'queue.sqlite3', store, gpu_lock=pathlib.Path(config['gpu_lock']),
                    handlers={'discovered': pipeline.stage, 'ready': pipeline.asr, 'publish_pending': Publisher()},
                    owner='nas-inbox-'+stage, budget=budget, reserve_bytes=lambda _:config['maximum_job'])
                runner.serve(stage, stop)
            except Exception as exc:
                errors.append(type(exc).__name__ + ':' + str(exc))
                stop.set()
            finally:
                if runner: runner.close()
                if budget: budget.close()
        threads = [threading.Thread(target=consume,args=(s,)) for s in ('discovered','ready','publish_pending')]
        for thread in threads: thread.start()
        queue = PipelineQueue(root/'queue.sqlite3')
        next_scan = time.monotonic() + config.get('scan_seconds', 300)
        try:
            while not stop.wait(5):
                counts = dict(queue.db.execute('select state,count(*) from pipeline_jobs group by state').fetchall())
                atomic_json(root/'health.json', {'updated_at':time.time(),'counts':counts,'errors':errors,'hub_delivery':'pending'})
                if config.get('continuous', False):
                    if time.monotonic() >= next_scan:
                        try:
                            report = pipeline.discover(queue)
                            atomic_json(root/'scan.json', report)
                        except (OSError, HoldJob) as exc:
                            atomic_json(root/'scan.json', {'error':str(exc)})
                        next_scan = time.monotonic() + config.get('scan_seconds', 300)
                elif not any(k not in ('done','held') for k in counts):
                    stop.set()
        finally:
            stop.set()
            for thread in threads: thread.join()
            queue.close()
        if errors: raise RuntimeError(';'.join(errors))
        return {'status':'stopped','scan':report}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=pathlib.Path, required=True)
    parser.add_argument('--scan-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(json.loads(args.config.read_bytes()), scan_only=args.scan_only), ensure_ascii=False))
