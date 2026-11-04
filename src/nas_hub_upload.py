#!/usr/bin/env python3
"""Explicit deferred Hub delivery of ONE verified NAS session, never ASR."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re

from hub_intake import Drive, DrivePermanent
from nas_inbox import inventory
from pipeline_runtime import HoldJob
from pipeline_storage import atomic_json, digest, no_links, safe_path

PUBLIC = {'.md', '.txt', '.json', '.jsonl', '.vtt'}


def md5_file(path):
    checksum = hashlib.md5()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def prepare(archive):
    archive = no_links(pathlib.Path(archive))
    state = json.loads((archive/'session.json').read_bytes())
    binding = state['binding']
    key = binding['job_id']
    if (not re.fullmatch(r'[a-f0-9]{64}', key) or archive.name != 'NAS-'+key or
            archive.parent.name != 'sessions' or state.get('status') != 'asr-done'):
        raise HoldJob('invalid_completed_nas_session')
    project = archive.parent.parent.name
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', project) or project == 'Gelya-Asr':
        raise HoldJob('invalid_nas_project')
    ready_path = archive/'receipts/receipt-ready.json'
    staged_path = archive/'receipts/receipt-discovered.json'
    ready = json.loads(ready_path.read_bytes())
    staged = json.loads(staged_path.read_bytes())
    if (ready.get('binding') != binding or staged.get('binding') != binding or
            ready.get('upstream_sha256') != digest(staged_path) or
            ready.get('stage') != 'ready' or staged.get('stage') != 'discovered' or
            ready.get('artifacts') != state['result_files']):
        raise HoldJob('nas_archive_receipt_changed')
    if inventory(archive/'source') != state['source_files']:
        raise HoldJob('nas_archive_source_changed')
    members = []
    for item in ready['artifacts']:
        rel = safe_path(item['path'])
        if not rel.is_relative_to(pathlib.PurePosixPath('result/transcripts')):
            raise HoldJob('invalid_result_path')
        relative = rel.relative_to('result').as_posix()
        local = no_links(archive/relative)
        if local.stat().st_size != item['size'] or digest(local) != item['sha256']:
            raise HoldJob('nas_archive_result_changed')
        if local.suffix.lower() in PUBLIC:
            members.append({'relative':relative, 'size':item['size'], 'sha256':item['sha256'],
                            'md5':md5_file(local)})
    if not any(x['relative'].endswith('-transcript.md') for x in members):
        raise HoldJob('missing_public_transcript')
    return {'project':project,'job_id':key,'binding':binding,'members':members,
            'target':project+'/sessions/nas/'+archive.name}


def upload(archive, drive, *, run=False):
    archive = pathlib.Path(archive)
    plan = prepare(archive)
    if not run:
        return {'status':'prepared','target':plan['target'],'files':len(plan['members']), 'asr_started':False}
    # Validate every existing path before writing anything. Never overwrite a
    # differing or ambiguous remote artifact in the job's immutable namespace.
    try:
        remote = drive.list(*plan['target'].split('/'), recursive=True)
    except DrivePermanent as exc:
        # rclone lsjson returns a nonzero code for a not-yet-created target.
        # Only its explicit directory-not-found response is absence evidence.
        if 'directory not found' not in str(exc).lower():
            raise
        remote = []
    paths = {}
    for entry in remote:
        if entry.get('IsDir'): continue
        if entry['Path'] in paths: raise HoldJob('duplicate_hub_result_path')
        paths[entry['Path']] = entry
    allowed = {x['relative'] for x in plan['members']} | {'delivery.json'}
    if set(paths)-allowed: raise HoldJob('foreign_hub_artifacts')
    def matches(entry, item):
        return (entry.get('Size') == item['size'] and
                (entry.get('Hashes',{}).get('md5') or '').lower() == item['md5'])
    for item in plan['members']:
        if item['relative'] in paths and not matches(paths[item['relative']], item):
            raise HoldJob('existing_hub_result_conflict')
    if 'delivery.json' in paths:
        old = json.loads(drive.call('cat',drive.path(plan['target'],'delivery.json')))
        if old != plan: raise HoldJob('hub_delivery_manifest_conflict')
    for item in plan['members']:
        if item['relative'] not in paths:
            local = archive/item['relative']
            drive.call('copyto',str(local),drive.path(plan['target'],item['relative']))
    def verify():
        entries = drive.list(*plan['target'].split('/'),recursive=True)
        for item in plan['members']:
            found = [e for e in entries if e.get('Path') == item['relative']]
            if len(found) != 1 or not matches(found[0],item):
                raise HoldJob('hub_readback_not_verified')
    verify()
    marker = archive/'hub-upload-manifest.json'
    if marker.exists() and json.loads(marker.read_bytes()) != plan:
        raise HoldJob('local_hub_manifest_changed')
    atomic_json(marker,plan)
    drive.call('copyto',str(marker),drive.path(plan['target'],'delivery.json'))
    if json.loads(drive.call('cat',drive.path(plan['target'],'delivery.json'))) != plan:
        raise HoldJob('hub_manifest_readback_failed')
    verify()
    atomic_json(archive/'hub-delivery.json',{'status':'delivered','job_id':plan['job_id'],
                'target':plan['target'],'verified':True})
    return {'status':'delivered','files':len(plan['members']),'asr_started':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True,type=pathlib.Path)
    parser.add_argument('--remote',default='gdrive:Hub')
    parser.add_argument('--rclone-config')
    parser.add_argument('--run',action='store_true')
    args = parser.parse_args()
    # Dry preparation does not construct/contact a Drive client.
    print(json.dumps(upload(args.session,Drive(args.remote,args.rclone_config) if args.run else None,run=args.run)))
