"""投取事件绑定核证（drop ↔ pickup bond proof）测试。

覆盖：正常关账写边、缺 drop 侧拒关账、库层禁止无边释放、
伪造/错型绑定边被拒、只读核证接口、存量补边迁移。
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app import seed as seed_mod
from app.modules import drop_pickup_bond_proof as bond_proof


def _connect(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lockerbay.db")
    monkeypatch.setattr(seed_mod, "connect", db_mod.connect)
    conn = db_mod.connect()
    conn.executescript(seed_mod.SCHEMA)
    conn.executescript(bond_proof.SCHEMA_SQL)
    return conn


def _add_event(conn, event_id, event_type, cell_id, tracking, ts):
    conn.execute(
        "INSERT INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no)"
        " VALUES (?,?,?,?,?,?)",
        (event_id, "CAB-A", cell_id, event_type, ts, tracking),
    )


def _open_occupancy(conn, occ_id, cell_id, tracking, ts, drop_id):
    conn.execute(
        "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, status, last_event_id)"
        " VALUES (?,?,?,?, 'occupied', ?)",
        (occ_id, cell_id, tracking, ts, drop_id),
    )


def _cell(conn, cell_id):
    conn.execute(
        "INSERT OR IGNORE INTO cabinets(id, label, site) VALUES ('CAB-A', 'A', 'site')"
    )
    conn.execute(
        "INSERT INTO cells(id, cabinet_id, code, status) VALUES (?, 'CAB-A', ?, 'free')",
        (cell_id, cell_id),
    )


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = _connect(tmp_path, monkeypatch)
    _cell(c, "CAB-A-01")
    _add_event(c, "D1", "drop", "CAB-A-01", "TN1", "2026-11-18T08:00:00Z")
    _open_occupancy(c, "OCC-1", "CAB-A-01", "TN1", "2026-11-18T08:00:00Z", "D1")
    c.execute("UPDATE cells SET status='occupied' WHERE id='CAB-A-01'")
    c.commit()
    yield c
    c.close()


def _pickup(event_id="P1", cell="CAB-A-01", tracking="TN1", ts="2026-11-18T12:00:00Z"):
    return {
        "id": event_id,
        "cell_id": cell,
        "tracking_no": tracking,
        "event_ts": ts,
        "event_type": "pickup",
    }


# ------------------------------------------------------------ 关账写路径


def test_close_writes_bond_and_releases(conn):
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    out = bond_proof.try_close_pickup(conn, _pickup())
    conn.commit()

    assert out["status"] == "closed"
    bond = conn.execute("SELECT * FROM drop_pickup_bonds").fetchone()
    assert bond["occupancy_id"] == "OCC-1"
    assert bond["drop_event_id"] == "D1"
    assert bond["pickup_event_id"] == "P1"
    assert bond["tracking_no"] == "TN1"
    assert bond["source"] == "close"
    assert conn.execute("SELECT status FROM occupancies WHERE id='OCC-1'").fetchone()[
        "status"
    ] == "released"
    assert conn.execute("SELECT status FROM cells WHERE id='CAB-A-01'").fetchone()[
        "status"
    ] == "free"


def test_missing_drop_leg_rejects_close(conn):
    _add_event(conn, "P9", "pickup", "CAB-A-01", "TN-OTHER", "2026-11-18T12:00:00Z")
    out = bond_proof.try_close_pickup(conn, _pickup(event_id="P9", tracking="TN-OTHER"))
    conn.commit()

    assert out["status"] == "rejected"
    assert out["code"] == "missing_drop_leg"
    assert conn.execute("SELECT COUNT(*) AS c FROM drop_pickup_bonds").fetchone()["c"] == 0
    assert conn.execute("SELECT status FROM occupancies WHERE id='OCC-1'").fetchone()[
        "status"
    ] == "occupied"
    assert conn.execute("SELECT status FROM cells WHERE id='CAB-A-01'").fetchone()[
        "status"
    ] == "occupied"
    q = conn.execute("SELECT * FROM quarantine").fetchone()
    assert q is not None
    assert bond_proof.REASON_MISSING_DROP in q["reason"]
    assert json.loads(q["member_json"])["pickup_event_id"] == "P9"


def test_no_open_occupancy_rejects_close(conn):
    # 格口不存在开放占用：拒关账并搁置，不硬开账。
    out = bond_proof.try_close_pickup(conn, _pickup(event_id="P2", cell="CAB-A-09"))
    assert out["status"] == "rejected"
    assert out["code"] == "no_open_occupancy"
    assert conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"] == 1


# ------------------------------------------------------------ 库层兜底


def test_db_rejects_release_without_bond(conn):
    with pytest.raises(sqlite3.IntegrityError, match="无绑定边"):
        conn.execute("UPDATE occupancies SET status='released' WHERE id='OCC-1'")


def test_db_rejects_inserting_released_occupancy(conn):
    with pytest.raises(sqlite3.IntegrityError, match="不得以 released 直接建账"):
        conn.execute(
            "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, status)"
            " VALUES ('OCC-RAW', 'CAB-A-01', 'TN1', '2026-11-18T08:00:00Z', 'released')"
        )


def test_db_rejects_bond_to_missing_event(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
            " pickup_event_id, tracking_no, created_at)"
            " VALUES ('B-X', 'OCC-1', 'NOPE', 'P1', 'TN1', '2026-11-18T12:00:00Z')"
        )


def test_db_rejects_bond_with_wrong_event_type(conn):
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    with pytest.raises(sqlite3.IntegrityError, match="drop 端"):
        conn.execute(
            "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
            " pickup_event_id, tracking_no, created_at)"
            " VALUES ('B-X', 'OCC-1', 'P1', 'P1', 'TN1', '2026-11-18T12:00:00Z')"
        )


def test_db_rejects_bond_tracking_mismatch(conn):
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    with pytest.raises(sqlite3.IntegrityError, match="运单"):
        conn.execute(
            "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
            " pickup_event_id, tracking_no, created_at)"
            " VALUES ('B-X', 'OCC-1', 'D1', 'P1', 'TN-WRONG', '2026-11-18T12:00:00Z')"
        )


def test_one_bond_per_occupancy(conn):
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    bond_proof.try_close_pickup(conn, _pickup())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
            " pickup_event_id, tracking_no, created_at)"
            " VALUES ('B-DUP', 'OCC-1', 'D1', 'P1', 'TN1', '2026-11-18T12:00:00Z')"
        )


def test_failed_close_leaves_no_partial_bond(conn):
    """写边后任一步失败必须整体回滚：不留孤儿绑定边，占用不得半 released。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.commit()

    class FlakyConn:
        """代理连接：改格口那一步抛错，其余透传。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *params):
            if str(sql).lstrip().upper().startswith("UPDATE CELLS"):
                raise RuntimeError("格口改态失败")
            return self._real.execute(sql, *params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    flaky = FlakyConn(conn)
    with pytest.raises(RuntimeError):
        with bond_proof.atomic(flaky, "t_fail"):
            occupancy = bond_proof.find_open_occupancy(flaky, "CAB-A-01")
            bond_proof.close_with_bond(flaky, occupancy, _pickup())

    assert conn.execute("SELECT COUNT(*) AS c FROM drop_pickup_bonds").fetchone()["c"] == 0
    assert conn.execute("SELECT status FROM occupancies WHERE id='OCC-1'").fetchone()[
        "status"
    ] == "occupied"
    assert conn.execute("SELECT status FROM cells WHERE id='CAB-A-01'").fetchone()[
        "status"
    ] == "occupied"


# ------------------------------------------------------------ 存量迁移


def test_backfill_repairs_legacy_released(conn):
    # 模拟旧逻辑：直接改 status 的存量（触发器先关掉以造出脏数据）
    conn.execute("DROP TRIGGER trg_released_requires_bond")
    conn.execute(
        "UPDATE occupancies SET status='released', closed_at='2026-11-18T12:00:00Z'"
        " WHERE id='OCC-1'"
    )
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.executescript(bond_proof.SCHEMA_SQL)  # 恢复触发器

    report = bond_proof.backfill_bonds(conn)
    assert report["backfilled"] == 1
    bond = conn.execute("SELECT * FROM drop_pickup_bonds").fetchone()
    assert bond["source"] == "backfill"
    assert bond["drop_event_id"] == "D1"
    assert bond["pickup_event_id"] == "P1"

    # 幂等：再跑一次不重复补
    again = bond_proof.backfill_bonds(conn)
    assert again["backfilled"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM drop_pickup_bonds").fetchone()["c"] == 1


def test_backfill_quarantines_when_unrepairable(tmp_path, monkeypatch):
    c = _connect(tmp_path, monkeypatch)
    _cell(c, "CAB-A-02")
    _add_event(c, "P-ONLY", "pickup", "CAB-A-02", "TNX", "2026-11-18T12:00:00Z")
    # 旧库没有新触发器，造出存量已释放行后再恢复 schema
    c.execute("DROP TRIGGER trg_occupancy_insert_not_released")
    c.execute(
        "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status)"
        " VALUES ('OCC-2', 'CAB-A-02', 'TNX', '2026-11-18T11:00:00Z',"
        " '2026-11-18T12:00:00Z', 'released')"
    )
    c.executescript(bond_proof.SCHEMA_SQL)  # 恢复触发器
    c.commit()
    report = bond_proof.backfill_bonds(c)
    assert report["backfilled"] == 0
    assert report["quarantined"] == 1
    q = c.execute("SELECT * FROM quarantine").fetchone()
    assert q["reason"] == bond_proof.REASON_BACKFILL
    c.close()


# ------------------------------------------------------------ 只读核证接口


def test_bond_lookup_by_occupancy(conn):
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    bond_proof.try_close_pickup(conn, _pickup())
    conn.commit()

    found = bond_proof.bond_for_occupancy(conn, "OCC-1")
    assert found["bond"]["drop_event_id"] == "D1"
    assert found["drop_event"]["id"] == "D1"
    assert found["drop_event"]["event_type"] == "drop"
    assert found["pickup_event"]["id"] == "P1"
    assert found["pickup_event"]["event_type"] == "pickup"


def test_bond_proofs_flags_anomaly(tmp_path, monkeypatch):
    c = _connect(tmp_path, monkeypatch)
    _cell(c, "CAB-A-03")
    c.execute("DROP TRIGGER trg_occupancy_insert_not_released")
    c.execute(
        "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status)"
        " VALUES ('OCC-3', 'CAB-A-03', 'TN3', '2026-11-18T08:00:00Z',"
        " '2026-11-18T09:00:00Z', 'released')"
    )
    c.executescript(bond_proof.SCHEMA_SQL)
    c.commit()
    rows = bond_proof.bond_proofs(c)
    assert len(rows) == 1
    assert rows[0]["bonded"] is False
    assert "缺绑定边" in rows[0]["anomaly"]
    c.close()


def test_http_bond_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lockerbay.db")
    monkeypatch.setattr(db_mod, "FIXTURES_DIR", Path(__file__).resolve().parents[2] / "fixtures")
    monkeypatch.setattr(seed_mod, "FIXTURES_DIR", db_mod.FIXTURES_DIR)

    from app.main import app

    with TestClient(app) as client:
        proofs = client.get("/api/bond-proofs").json()
        a01 = [p for p in proofs if p["occupancy_id"] == "OCC-CAB-A-01-E1"]
        assert a01 and a01[0]["bonded"] is True
        assert a01[0]["drop_event"]["id"] == "E1"
        assert a01[0]["pickup_event"]["id"] == "E3"

        detail = client.get("/api/occupancy-bonds/OCC-CAB-A-01-E1")
        assert detail.status_code == 200
        body = detail.json()
        assert body["drop_event"]["id"] == "E1"
        assert body["pickup_event"]["id"] == "E3"

        open_occ = [p for p in proofs if p["status"] == "occupied"][0]
        pending = client.get(f"/api/occupancy-bonds/{open_occ['occupancy_id']}")
        assert pending.status_code == 200
        assert pending.json()["bond"] is None
        assert pending.json()["expected"] == "closed_on_pickup"


def test_http_ingest_bonds_and_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lockerbay.db")
    fixtures = Path(__file__).resolve().parents[2] / "fixtures"
    monkeypatch.setattr(db_mod, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(seed_mod, "FIXTURES_DIR", fixtures)

    from app.main import app

    with TestClient(app) as client:
        report = client.post("/api/ingest/run").json()
        assert report["bonded"] == 1
        assert len(report["rejected_closes"]) == 1
        assert report["rejected_closes"][0]["pickup_event_id"] == "E7"

        bond = client.get("/api/occupancy-bonds/OCC-CAB-A-03-E5")
        assert bond.status_code == 200
        assert bond.json()["pickup_event"]["id"] == "E6"

        quarantine = client.get("/api/quarantine").json()
        assert any("拒绝关账" in q["reason"] for q in quarantine)
