import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from pipeline_queue import LeaseLost, PipelineQueue


def put(q, revision="r1"):
    return q.enqueue("collection/meeting", revision, "profile", "engine", {"files": ["a.m4a"]})


def test_idempotency_and_revision(tmp_path):
    q = PipelineQueue(tmp_path / "queue.db")
    try:
        key = put(q)
        lease = q.claim("discovered", "stager")
        assert put(q) == key
        assert q.get(key)["token"] == lease["token"]
        assert put(q, "r2") != key
        with pytest.raises(ValueError, match="different payload"):
            q.enqueue("collection/meeting", "r1", "profile", "engine", {"files": []})
    finally:
        q.close()


def test_stage_transitions_and_fences(tmp_path):
    q = PipelineQueue(tmp_path / "q.db")
    try:
        key = put(q)
        for stage, active in [("discovered", "staging"), ("ready", "transcribing"),
                              ("publish_pending", "publishing")]:
            lease = q.claim(stage, "worker")
            assert lease["state"] == active
            with pytest.raises(LeaseLost):
                q.finish(key, "wrong-token")
            q.heartbeat(key, lease["token"])
            q.finish(key, lease["token"])
            with pytest.raises(LeaseLost):
                q.finish(key, lease["token"])
        assert q.get(key)["state"] == "done"
        assert q.claim("publish_pending", "worker") is None
    finally:
        q.close()


def test_expiration_never_steals_live_resource(tmp_path):
    when = [100.0]
    q = PipelineQueue(tmp_path / "q.db", clock=lambda: when[0])
    try:
        key = put(q)
        lease = q.claim("discovered", "a", lease_seconds=10)
        when[0] = 110
        assert q.expired()[0]["id"] == key
        assert q.claim("discovered", "b") is None
        for action in [q.finish, q.heartbeat]:
            with pytest.raises(LeaseLost):
                action(key, lease["token"])
    finally:
        q.close()


def test_retry_keeps_stage_and_blocks_stale_completion(tmp_path):
    when = [100.0]
    q = PipelineQueue(tmp_path / "q.db", clock=lambda: when[0])
    try:
        key = put(q)
        lease = q.claim("discovered", "a")
        q.fail(key, lease["token"], "nas_offline", retry_seconds=10)
        assert q.claim("discovered", "b") is None
        when[0] += 11
        new = q.claim("discovered", "b")
        with pytest.raises(LeaseLost):
            q.finish(key, lease["token"])
        q.fail(key, new["token"], "checksum_conflict")
        assert q.get(key)["state"] == "held"
        assert q.claim("discovered", "c") is None
    finally:
        q.close()


def test_two_connections_one_winner_and_restart(tmp_path):
    path = tmp_path / "q.db"
    q = PipelineQueue(path)
    key = put(q)
    q.close()
    def claim(owner):
        other = PipelineQueue(path)
        try:
            return other.claim("discovered", owner)
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["a", "b"]))
    assert sum(x is not None for x in results) == 1
    q = PipelineQueue(path)
    try:
        assert q.get(key)["state"] == "staging"
    finally:
        q.close()


def test_refuses_legacy_db(tmp_path):
    path = tmp_path / "q.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (key TEXT)")
    db.close()
    with pytest.raises(ValueError, match="isolated"):
        PipelineQueue(path)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_lease(tmp_path, duration):
    q = PipelineQueue(tmp_path / "q.db")
    try:
        put(q)
        with pytest.raises(ValueError):
            q.claim("discovered", "a", lease_seconds=duration)
    finally:
        q.close()
