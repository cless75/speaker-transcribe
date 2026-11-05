import json
import pathlib

import pytest

from nas_inbox import NasInboxPipeline, copy_verified, inventory, select_input_plan, select_primary
from pipeline_queue import PipelineQueue
from pipeline_runtime import HoldJob, StageRunner
from pipeline_storage import ReceiptStore, required_destinations, EvidenceError
from test_hub_worker import fake_asr


def setup(tmp_path):
    heavy = tmp_path / 'Heavy'
    source = heavy / '_inbox' / '506' / 'meeting'
    source.mkdir(parents=True)
    (source / 'audio.m4a').write_bytes(b'mixed audio')
    (source / 'video.mp4').write_bytes(b'video')
    participants = source / 'Audio Record'
    participants.mkdir()
    (participants / 'notes.txt').write_bytes(b'participant notes')
    cfg = {'heavy_root': str(heavy), 'inbox_root': str(heavy/'_inbox'),
           'mountpoint': str(tmp_path), 'test_local_filesystem': True,
           'maximum_job':20*1024**3, 'settle_seconds':0}
    store = ReceiptStore(tmp_path/'state')
    calls = []
    fake = fake_asr(calls)
    def executor(*args):
        path, meta = fake(*args)
        meta['outputs'] = {'md':str(path),'run_meta':str(next(path.parent.glob('*-run-meta.json')))}
        meta['voiceprint'] = {'mode':'off','enabled':False}
        return path, meta
    pipeline = NasInboxPipeline(cfg, store, {'hub_root':str(heavy)},tmp_path/'node.json',
                  executor=executor, probe=lambda p:60, gpu_check=lambda:None)
    runner = StageRunner(tmp_path/'queue.db',store,gpu_lock=tmp_path/'gpu.lock',owner='test',
             handlers={'discovered':pipeline.stage,'ready':pipeline.asr,'publish_pending':pipeline.publish})
    return source,pipeline,runner,calls


def test_offline_end_to_end_archives_whole_bundle_and_reuses(tmp_path):
    source,p,r,calls = setup(tmp_path)
    try:
        original = inventory(source)
        assert p.discover(r.queue)['candidates']==1
        assert p.discover(r.queue)['candidates']==0
        job=r.queue.db.execute('select id from pipeline_jobs').fetchone()[0]
        assert required_destinations(r.queue.get(job))==('nas',)
        assert r.tick('discovered')=='finished'
        assert r.tick('ready')=='finished'
        assert r.tick('publish_pending')=='finished'
        archive=p.archive(r.queue.get(job))
        assert not source.exists()
        assert inventory(archive/'source')==original
        assert json.loads((archive/'hub-delivery.json').read_text())['status']=='pending'
        assert p.verify_completed(r.queue.get(job),None,lambda:None)
        assert p.discover(r.queue)['candidates']==0
        assert len(calls)==1
    finally:r.close()


def test_prior_results_hold_whole_meeting(tmp_path):
    source,p,r,_=setup(tmp_path)
    try:
        (source/'old-run-meta.json').write_text('{}')
        report=p.discover(r.queue)
        assert report['candidates']==0
        assert report['held'][0]['reason']=='prior_processing_requires_reconciliation'
    finally:r.close()


def test_no_multiple_audio_jobs(tmp_path):
    with pytest.raises(HoldJob,match='ambiguous'):
        select_primary([{'path':'a.m4a'},{'path':'b.m4a'}])


def test_verified_interrupted_copy_is_resumed_without_overwrite(tmp_path):
    import hashlib
    source=tmp_path/'source'; source.write_bytes(b'payload')
    target=tmp_path/'out'/'result'; target.parent.mkdir()
    temp=target.with_name('.copy-'+target.name); temp.write_bytes(b'payload')
    expected={'size':7,'sha256':hashlib.sha256(b'payload').hexdigest()}
    copy_verified(source,target,expected,lambda:None)
    assert target.read_bytes() == b'payload' and not temp.exists()


def test_conflicting_interrupted_copy_is_preserved(tmp_path):
    import hashlib
    source=tmp_path/'source'; source.write_bytes(b'payload')
    target=tmp_path/'out'/'result'; target.parent.mkdir()
    temp=target.with_name('.copy-'+target.name); temp.write_bytes(b'corrupt')
    expected={'size':7,'sha256':hashlib.sha256(b'payload').hexdigest()}
    with pytest.raises(HoldJob,match='partial_copy_requires_reconciliation'):
        copy_verified(source,target,expected,lambda:None)
    assert temp.read_bytes() == b'corrupt' and not target.exists()


def test_interrupted_copy_is_not_promoted_if_ownership_lost(tmp_path):
    import hashlib
    source=tmp_path/'source'; source.write_bytes(b'payload')
    target=tmp_path/'result'; temp=tmp_path/'.copy-result'; temp.write_bytes(b'payload')
    expected={'size':7,'sha256':hashlib.sha256(b'payload').hexdigest()}
    def lost(): raise HoldJob('lease_lost')
    with pytest.raises(HoldJob,match='lease_lost'):
        copy_verified(source,target,expected,lost)
    assert temp.exists() and not target.exists()


def test_vtt_has_priority_over_tracks_and_mixed_audio():
    files = [{'path':'audio.m4a'}, {'path':'Audio Record/a.m4a'}, {'path':'Audio Record/b.m4a'}, {'path':'recording.transcript.vtt'}]
    plan = select_input_plan(files)
    assert plan['mode'] == 'zoom_vtt' and plan['sidecars'] == ['recording.transcript.vtt']


def test_multiple_participant_tracks_skip_mixed_diarization():
    plan = select_input_plan([{'path':'audio.m4a'}, {'path':'Audio Record/a.m4a'}, {'path':'Audio Record/b.m4a'}])
    assert plan['mode'] == 'participant_tracks'
    assert plan['tracks'] == ['Audio Record/a.m4a', 'Audio Record/b.m4a']


def test_single_participant_track_never_silently_uses_mixed():
    assert select_input_plan([{'path':'audio.m4a'}, {'path':'Audio Record/a.m4a'}])['mode'] == 'participant_tracks'


def test_safe_service_holds_unverified_participant_export_before_enqueue(tmp_path):
    source,p,r,calls=setup(tmp_path)
    (source/'Audio Record'/'person.m4a').write_bytes(b'participant')
    p.config['safe_continuous_intake']=True
    try:
        report=p.discover(r.queue)
        assert report['candidates']==0
        assert report['held'][0]['reason']=='participant_alignment_and_coverage_required'
        assert not calls
    finally:r.close()


def test_safe_service_rejects_invalid_vtt_before_enqueue(tmp_path):
    source,p,r,calls=setup(tmp_path)
    (source/'recording.vtt').write_text('not a named Zoom transcript')
    p.config['safe_continuous_intake']=True
    try:
        report=p.discover(r.queue)
        assert report['candidates']==0
        assert report['held'][0]['reason']=='invalid_zoom_vtt'
    finally:r.close()


def test_historical_job_blocks_same_source_in_new_state(tmp_path):
    source,p,r,calls=setup(tmp_path)
    try:
        p.discover(r.queue)
        other=PipelineQueue(tmp_path/'fresh.db')
        try:
            p.config['history_queues']=[str(tmp_path/'queue.db')]
            report=p.discover(other)
            assert report['candidates']==0
            assert report['held'][0]['reason']=='historical_submission_requires_reconciliation'
        finally:other.close()
    finally:r.close()


def test_missing_history_queue_fails_closed(tmp_path):
    source,p,r,calls=setup(tmp_path)
    p.config['history_queues']=[str(tmp_path/'missing.db')]
    try:
        with pytest.raises(HoldJob,match='history_queue_missing'):p.discover(r.queue)
    finally:r.close()


def test_service_stop_fences_worker_checks(tmp_path):
    import threading
    source,p,r,calls=setup(tmp_path)
    p.stop_requested=threading.Event();p.stop_requested.set()
    try:
        with pytest.raises(HoldJob,match='service_stop_requested'):p.check({})
    finally:r.close()


def test_tracks_need_explicit_identity_and_voiceprint_consent(tmp_path):
    source,p,r,_=setup(tmp_path)
    (source/'Audio Record'/'person.m4a').write_bytes(b'participant one')
    (source/'Audio Record'/'person-two.m4a').write_bytes(b'participant two')
    try:
        p.discover(r.queue)
        assert r.tick('discovered') == 'finished'
        assert r.tick('ready') == 'held'
        assert r.queue.db.execute('select reason from pipeline_jobs').fetchone()[0] == 'participant_track_identity_required'
    finally:r.close()


def test_participant_tracks_are_transcribed_and_merged_without_diarization(tmp_path):
    source,p,r,calls=setup(tmp_path)
    (source/'Audio Record'/'person.m4a').write_bytes(b'participant one')
    second = source/'Audio Record'/'person-two.m4a'
    second.write_bytes(b'participant two')
    p.config['track_identities'] = {
        'Audio Record/person.m4a': {'display_name':'Alice', 'voiceprint_consent':True},
        'Audio Record/person-two.m4a': {'display_name':'Bob', 'voiceprint_consent':True},
    }
    def track_executor(audio, output, cfg, config_path, project):
        calls.append((audio.name, cfg['speaker_mode'], cfg['voiceprint_mode'], cfg['voiceprint_enroll_name']))
        output.mkdir(parents=True, exist_ok=True)
        base=audio.stem
        transcript=output/(base+'-transcript.md'); transcript.write_text('words')
        (output/(base+'-segments.jsonl')).write_text(json.dumps({'start':0,'end':1,'text':audio.stem})+'\n')
        store=pathlib.Path(cfg['machine_local_voiceprint_store_path']); store.parent.mkdir(parents=True,exist_ok=True)
        store.write_text(json.dumps({'persons':[cfg['voiceprint_enroll_name']]}))
        return transcript, {'status':'ok','voiceprint':{'enabled':True,'mode':'enroll'}}
    p.executor=track_executor
    try:
        p.discover(r.queue)
        job=r.queue.get(r.queue.db.execute('select id from pipeline_jobs').fetchone()[0])
        assert r.tick('discovered') == r.tick('ready') == 'finished'
        assert sorted(calls) == [('person-two.m4a','off','enroll','Bob'), ('person.m4a','off','enroll','Alice')]
        merged=(p.store.directory(job['id'])/'result/transcripts/session-transcript.md').read_text(encoding='utf8')
        assert 'Alice' in merged and 'Bob' in merged and 'без диаризации' in merged
        enrollment=json.loads((p.store.directory(job['id'])/'result/transcripts/Speakers/voiceprint-enrollment.json').read_text(encoding='utf8'))
        assert sorted(x['display_name'] for x in enrollment['profiles']) == ['Alice','Bob']
        assert (p.store.directory(job['id'])/'result/transcripts/Speakers/voiceprints.json').is_file()
    finally:r.close()


def test_input_change_never_stages(tmp_path):
    source,p,r,_=setup(tmp_path)
    try:
        p.discover(r.queue)
        (source/'audio.m4a').write_bytes(b'changed')
        assert r.tick('discovered')=='held'
    finally:r.close()


def test_archive_conflict_never_removes_source(tmp_path):
    source,p,r,_=setup(tmp_path)
    try:
        p.discover(r.queue)
        job=r.queue.db.execute('select id from pipeline_jobs').fetchone()[0]
        assert r.tick('discovered')==r.tick('ready')=='finished'
        target=p.archive(r.queue.get(job))/'source'; target.mkdir(parents=True)
        assert r.tick('publish_pending')=='held'
        assert source.exists()
    finally:r.close()


def test_nas_only_not_allowed_for_arbitrary_adapter():
    with pytest.raises(EvidenceError):
        required_destinations({'payload':json.dumps({'adapter':'hub-nas-v1','delivery_policy':'nas-only'})})


def test_mount_guard_allows_autofs_overlay_but_checks_actual_cifs(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import nas_inbox
    _,p,r,_=setup(tmp_path)
    try:
        p.config.update(test_local_filesystem=False,mount_source='//nas/Archive',nas_minimum_free=0)
        entries=[{'target':str(tmp_path),'source':'systemd-1','fstype':'autofs'},
                 {'target':str(tmp_path),'source':'//nas/Archive','fstype':'cifs'}]
        monkeypatch.setattr(nas_inbox.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps({'filesystems':entries})))
        p.guard()
        entries[1]['source']='//wrong/share'
        with pytest.raises(HoldJob,match='mount_identity'):p.guard()
    finally:r.close()


def test_same_job_in_other_state_root_cannot_adopt_source_claim(tmp_path):
    source,p,r,_=setup(tmp_path)
    try:
        p.discover(r.queue)
        job=r.queue.get(r.queue.db.execute('select id from pipeline_jobs').fetchone()[0])
        p.acquire(job)
        other=NasInboxPipeline(p.config,ReceiptStore(tmp_path/'other-state'),p.cfg,p.config_path)
        with pytest.raises(HoldJob,match='source_claim_changed'):
            other.acquire(job)
        p.check(job)
    finally:r.close()


def test_publish_resume_after_source_rename_does_not_repeat_asr(tmp_path, monkeypatch):
    import nas_inbox
    source,p,r,calls=setup(tmp_path)
    try:
        p.discover(r.queue)
        job=r.queue.get(r.queue.db.execute('select id from pipeline_jobs').fetchone()[0])
        assert r.tick('discovered')==r.tick('ready')=='finished'
        original=nas_inbox.atomic_json
        def interrupt(path,value):
            if path.name=='session.json':raise OSError('interrupted after rename')
            return original(path,value)
        monkeypatch.setattr(nas_inbox,'atomic_json',interrupt)
        with pytest.raises(OSError,match='after rename'):
            p.publish(job,p.store.directory(job['id']),lambda:None)
        assert not source.exists()
        monkeypatch.setattr(nas_inbox,'atomic_json',original)
        assert p.publish(job,p.store.directory(job['id']),lambda:None)['deliveries']['nas']['verified']
        assert p.verify_completed(job,None,lambda:None)
        assert len(calls)==1
    finally:r.close()


def test_corrupt_archived_result_fails_verification(tmp_path):
    source,p,r,_=setup(tmp_path)
    try:
        p.discover(r.queue)
        job=r.queue.get(r.queue.db.execute('select id from pipeline_jobs').fetchone()[0])
        assert r.tick('discovered')==r.tick('ready')==r.tick('publish_pending')=='finished'
        artifact=next((p.archive(job)/'transcripts').glob('*transcript.md'))
        artifact.write_text('corrupt')
        with pytest.raises(HoldJob,match='archived_result_changed'):
            p.verify_completed(job,None,lambda:None)
    finally:r.close()
