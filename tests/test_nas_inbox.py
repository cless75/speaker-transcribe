import json

import pytest

from nas_inbox import NasInboxPipeline, inventory, select_primary
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
    (participants / 'person.m4a').write_bytes(b'participant')
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
