import json
import sys

import pytest

import audio_inbox_watch as watcher
from pipeline_process import AsrProcess
from pipeline_runtime import HoldJob


def test_real_owned_child_records_confirmed_exit(tmp_path):
    cli = tmp_path / "worker.py"
    cli.write_text(
        "import pathlib,sys,json\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('--output-dir')+1])\n"
        "(out/'clip-transcript.md').write_text('synthetic')\n"
        "print('phase=stdout_json ok',file=sys.stderr)\n"
        "print(json.dumps({'status':'ok','outputs':{'md':'clip-transcript.md'}}))\n")
    calls = []
    control = AsrProcess(tmp_path, check=lambda: calls.append("stage"),
                         ownership_check=lambda: calls.append("owner"))
    result = watcher.run_asr(tmp_path / "clip.wav",
                             {"transcribe_python": sys.executable, "transcribe_cli": str(cli),
                              "speaker_mode": "off"}, "Example", tmp_path, tmp_path / "out",
                             tmp_path / "node.json", process_control=control)
    assert result[0] is True
    record = json.loads((tmp_path / "asr-process.json").read_bytes())
    assert record["finished"] is True and record["returncode"] == 0
    assert "stage" in calls and "owner" in calls


def test_lost_ownership_terminates_only_its_spawned_worker(tmp_path):
    cli = tmp_path / "worker.py"
    cli.write_text("import time\ntime.sleep(60)\n")
    control = AsrProcess(tmp_path, check=lambda: None,
                         ownership_check=lambda: (_ for _ in ()).throw(HoldJob("ownership_lost")))
    with pytest.raises(HoldJob, match="ownership_lost"):
        watcher.run_asr(tmp_path / "clip.wav",
                        {"transcribe_python": sys.executable, "transcribe_cli": str(cli),
                         "speaker_mode": "off"}, "Example", tmp_path, tmp_path / "out",
                        tmp_path / "node.json", process_control=control)
    record = json.loads((tmp_path / "asr-process.json").read_bytes())
    assert record["finished"] is True and record["returncode"] != 0
