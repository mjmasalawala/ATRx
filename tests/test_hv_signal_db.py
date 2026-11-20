"""db_store.save_hv_signal_target_status: merges each patch into the
signal's `metrics` JSON in one batched statement. A fake connection records
the SQL; nothing touches a real database."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_store


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, params):
        self.log.append(("executemany", sql, params))


class FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        self.log.append(("commit",))


def test_empty_updates_do_not_touch_the_database(monkeypatch):
    monkeypatch.setattr(db_store, "get_conn", lambda: (_ for _ in ()).throw(AssertionError("no db call expected")))
    db_store.save_hv_signal_target_status([])


def test_patches_are_merged_into_metrics_in_one_round_trip(monkeypatch):
    log = []
    monkeypatch.setattr(db_store, "get_conn", lambda: FakeConn(log))
    db_store.save_hv_signal_target_status([
        {"id": 4, "patch": {"target_status": "hit", "target_hit_at": "2026-11-19T10:15:00"}},
        {"id": 9, "patch": {"target_checked": "2026-11-19"}},
    ])
    kind, sql, params = log[0]
    assert kind == "executemany" and log[1] == ("commit",)
    assert "metrics = COALESCE(metrics" in sql and "|| %s::jsonb" in sql and "WHERE id = %s" in sql
    assert [p[1] for p in params] == [4, 9]
    assert params[0][0].obj == {"target_status": "hit", "target_hit_at": "2026-11-19T10:15:00"}
    assert params[1][0].obj == {"target_checked": "2026-11-19"}
