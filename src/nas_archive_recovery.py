#!/usr/bin/env python3
"""Explicit publication-only recovery of a pinned legacy NAS Inbox job.

No discovery, ASR, claim adoption, service changes, or automatic retry. Defaults
to verification only; --run backs up evidence before resuming one held job.
The supplied legacy release is checksum-verified and remains unmodified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import time


def verify_release(release):
    manifest = json.loads((release / 'manifest.json').read_bytes())
    for name, expected in manifest['files'].items():
        rel = pathlib.PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('unsafe_release_manifest')
        path = release / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('legacy_release_changed')
    return hashlib.sha256((release / 'manifest.json').read_bytes()).hexdigest()


def verify_archive(archive, ready, binding, digest, no_links):
    """Only exact receipted results/temps or exact control evidence are allowed."""
    allowed = {}
    for member in ready['artifacts']:
        rel = pathlib.PurePosixPath(member['path']).relative_to('result')
        if '..' in rel.parts:
            raise ValueError('unsafe_receipt_path')
        allowed[rel.as_posix()] = member
        allowed[(rel.parent / ('.copy-' + rel.name)).as_posix()] = member
    for path in archive.rglob('*'):
        no_links(path)
        if not path.is_file():
            continue
        relative = path.relative_to(archive).as_posix()
        if relative == 'archive-binding.json':
            if json.loads(path.read_bytes()) != binding:
                raise ValueError('archive_binding_conflict')
            continue
        member = allowed.get(relative)
        if member is None:
            raise ValueError('unexpected_legacy_archive_file')
        if path.stat().st_size != member['size'] or digest(path, strict_snapshot=False) != member['sha256']:
            raise ValueError('legacy_archive_content_conflict')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=pathlib.Path, required=True)
    parser.add_argument('--config', type=pathlib.Path, required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    release = args.release.resolve(strict=True)
    release_digest = verify_release(release)
    # Use exactly the original frozen engine and claim contract, not the newer
    # scanner/ASR implementation. This tool changes publication behavior only.
    sys.path.insert(0, str(release / 'src'))
    import nas_inbox as legacy
    from pipeline_storage import ReceiptStore, atomic_json, digest, file_lock, no_links, sync_directory
    from pipeline_queue import PipelineQueue
    from pipeline_budget import DiskBudget
    from pipeline_runtime import StageRunner, HoldJob

    def copy_verified(source, target, expected, check):
        target = no_links(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        def matches(path):
            return (path.stat().st_size == expected['size'] and
                    digest(path, strict_snapshot=False) == expected['sha256'])
        check()
        if target.exists():
            if not matches(target): raise HoldJob('existing_copy_conflict')
            return
        temp = no_links(target.with_name('.copy-' + target.name))
        if not temp.exists():
            with source.open('rb') as inp, temp.open('xb') as out:
                for block in iter(lambda: inp.read(1024 * 1024), b''):
                    check(); out.write(block)
                out.flush(); os.fsync(out.fileno())
        if not matches(temp): raise HoldJob('partial_copy_requires_reconciliation')
        check()
        if target.exists(): raise HoldJob('copy_destination_appeared')
        os.replace(temp, target)
        sync_directory(target.parent)

    legacy.copy_verified = copy_verified
    cfg = json.loads(args.config.read_bytes())
    root = no_links(pathlib.Path(cfg['state_root']))
    store = ReceiptStore(root)
    node_path = root / 'node-snapshot.json'
    pipeline = legacy.NasInboxPipeline(cfg, store, json.loads(node_path.read_bytes()), node_path)
    queue = PipelineQueue(root / 'queue.sqlite3')
    budget = runner = None
    try:
        with file_lock(root / 'supervisor.lock'):
            job = queue.get(args.job)
            if not job or job['state'] != 'held' or job['resume_stage'] != 'publish_pending' or job['reason'] != 'artifact_changed' or job['token'] is not None:
                raise ValueError('expected_unowned_held_publication_job')
            with file_lock(store.directory(args.job) / 'resource.lock'):
                pipeline.check(job)
                ready = store.read(job, 'ready')
                if ready is None or store.path(args.job, 'publish_pending').exists():
                    raise ValueError('expected_ready_without_publication_receipt')
                data, _ = pipeline.source(job)
                if legacy.inventory(pipeline.source_root(data)) != data['files']:
                    raise ValueError('legacy_source_changed')
                archive = pipeline.archive(job)
                verify_archive(archive, ready, store.binding(job), digest, no_links)
                evidence = {'job': job, 'release': str(release), 'release_manifest_sha256': release_digest,
                            'ready_sha256': digest(store.path(args.job, 'ready')),
                            'asr_log_sha256': digest(store.directory(args.job) / 'private-asr.log'),
                            'archive': str(archive), 'asr_started': False}
                if not args.run:
                    print(json.dumps({'status': 'verified', **evidence})); return
                backup = root / 'recovery' / (args.job + '-' + str(time.time_ns()))
                backup.mkdir(parents=True, exist_ok=False)
                with sqlite3.connect(backup / 'queue.sqlite3') as target:
                    queue.db.backup(target)
                atomic_json(backup / 'preflight.json', evidence)
                # Bind this exact inspected partial archive before adopting any
                # of its already verified files. Preserve the old claim format.
                archive.mkdir(parents=True, exist_ok=True)
                atomic_json(archive / 'archive-binding.json', store.binding(job))
                with queue._transaction():
                    if queue.get(args.job) != job:
                        raise ValueError('job_changed_during_preflight')
                    queue.db.execute("UPDATE pipeline_jobs SET state='publish_pending',reason=NULL,eligible=? WHERE id=?",
                                     (time.time(), args.job))
            def forbidden(*unused): raise HoldJob('publication_only_recovery')
            class Publisher:
                def __call__(self, *values): return pipeline.publish(*values)
                def verify_completed(self, *values): return pipeline.verify_completed(*values)
            budget = DiskBudget(pathlib.Path(cfg['budget_path']), capacity=cfg['buffer_capacity'],
                                minimum_free=cfg['local_minimum_free'], maximum_job=cfg['maximum_job'],
                                volume=pathlib.Path(cfg['budget_volume']))
            runner = StageRunner(root / 'queue.sqlite3', store, gpu_lock=pathlib.Path(cfg['gpu_lock']),
                                 handlers={'discovered': forbidden, 'ready': forbidden, 'publish_pending': Publisher()},
                                 owner='nas-publication-recovery', job_key=args.job, budget=budget,
                                 reserve_bytes=lambda _: cfg['maximum_job'])
            outcome = runner.tick('publish_pending')
            current = queue.get(args.job)
            if outcome != 'finished' or current['state'] != 'done':
                raise RuntimeError('publication_not_finished:' + str(current['reason']))
            pipeline.verify_completed(current, store.read(current, 'publish_pending'), lambda: None)
            if digest(store.directory(args.job) / 'private-asr.log') != evidence['asr_log_sha256']:
                raise RuntimeError('asr_log_changed')
            atomic_json(backup / 'outcome.json', {'status': 'done', 'job_id': args.job, 'asr_started': False})
            print(json.dumps({'status': 'done', 'job_id': args.job, 'archive': str(archive), 'backup': str(backup), 'asr_started': False}))
    finally:
        if runner: runner.close()
        if budget: budget.close()
        queue.close()


if __name__ == '__main__':
    main()
