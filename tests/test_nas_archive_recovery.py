import hashlib
import json

import pytest

from nas_archive_recovery import verify_archive, verify_release
from pipeline_storage import digest, no_links


def test_legacy_release_is_verified(tmp_path):
    (tmp_path/'code.py').write_bytes(b'original')
    manifest={'files':{'code.py':hashlib.sha256(b'original').hexdigest()}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    assert verify_release(tmp_path)
    (tmp_path/'code.py').write_bytes(b'changed')
    with pytest.raises(ValueError,match='legacy_release_changed'): verify_release(tmp_path)


def test_archive_only_accepts_receipted_partial_copy(tmp_path):
    folder=tmp_path/'transcripts'; folder.mkdir()
    temp=folder/'.copy-result.json'; temp.write_bytes(b'expected')
    ready={'artifacts':[{'path':'result/transcripts/result.json','size':8,
                         'sha256':hashlib.sha256(b'expected').hexdigest()}]}
    verify_archive(tmp_path,ready,{},digest,no_links)
    temp.write_bytes(b'conflict')
    with pytest.raises(ValueError,match='content_conflict'): verify_archive(tmp_path,ready,{},digest,no_links)


def test_archive_unknown_file_blocks_reconciliation(tmp_path):
    (tmp_path/'foreign').write_bytes(b'foreign')
    with pytest.raises(ValueError,match='unexpected_legacy_archive_file'):
        verify_archive(tmp_path,{'artifacts':[]},{},digest,no_links)
