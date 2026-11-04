import pytest

from nas_hub_upload import upload
from pipeline_runtime import HoldJob
from test_nas_inbox import setup
from test_hub_worker import MemoryDrive


def completed(tmp_path):
    _,p,r,calls=setup(tmp_path)
    p.discover(r.queue)
    job=r.queue.get(r.queue.db.execute('select id from pipeline_jobs').fetchone()[0])
    assert r.tick('discovered')==r.tick('ready')==r.tick('publish_pending')=='finished'
    return p.archive(job),r,calls


def test_offline_prepare_never_contacts_drive(tmp_path):
    archive,r,calls=completed(tmp_path)
    try:
        assert upload(archive,None)['status']=='prepared'
        assert len(calls)==1
    finally:r.close()


def test_delivery_retries_without_asr(tmp_path):
    archive,r,calls=completed(tmp_path)
    try:
        drive=MemoryDrive({})
        assert upload(archive,drive,run=True)['status']=='delivered'
        original=dict(drive.files)
        assert upload(archive,drive,run=True)['status']=='delivered'
        assert drive.files==original
        assert len(calls)==1
    finally:r.close()


def test_foreign_remote_artifact_blocks_before_upload(tmp_path):
    archive,r,calls=completed(tmp_path)
    try:
        target='506/sessions/nas/'+archive.name
        drive=MemoryDrive({target+'/foreign.txt':b'foreign'})
        original=dict(drive.files)
        with pytest.raises(HoldJob,match='foreign_hub_artifacts'):
            upload(archive,drive,run=True)
        assert drive.files==original
    finally:r.close()


def test_changed_archive_blocks_even_dry_run(tmp_path):
    archive,r,calls=completed(tmp_path)
    try:
        (archive/'source/audio.m4a').write_bytes(b'changed')
        with pytest.raises(HoldJob,match='archive_source_changed'):
            upload(archive,None)
    finally:r.close()


def test_partial_upload_resumes_without_marking_early_completion(tmp_path):
    import json
    archive,r,calls=completed(tmp_path)
    class Interrupted(MemoryDrive):
        fail=True
        def call(self,*args):
            result=super().call(*args)
            if self.fail and args[0]=='copyto':
                self.fail=False
                raise OSError('ambiguous network failure after copy')
            return result
    try:
        drive=Interrupted({})
        with pytest.raises(OSError):upload(archive,drive,run=True)
        assert json.loads((archive/'hub-delivery.json').read_text())['status']=='pending'
        assert upload(archive,drive,run=True)['status']=='delivered'
        assert len(calls)==1
    finally:r.close()
