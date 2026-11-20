"""占用事件链投影（occupancy event chain projection）测试。

覆盖：关账同成败落链、链与绑定边互证、链序与时序不分叉、
人为抽 pickup 腿必败、库层兜底触发器、存量补链、
格阵下钻与核证只读接口、装载关账回执双载。
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app import seed as seed_mod
from app.modules import drop_pickup_bond_proof as bond_proof
from app.modules import occupancy_event_chain as chain_proj


def _connect(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lockerbay.db")
    monkeypatch.setattr(seed_mod, "connect", db_mod.connect)
    conn = db_mod.connect()
    conn.executescript(seed_mod.SCHEMA)
    conn.executescript(bond_proof.SCHEMA_SQL)
    conn.executescript(chain_proj.SCHEMA_SQL)
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


def _close(conn, pickup=None):
    return bond_proof.try_close_pickup(conn, pickup or _pickup())


# ------------------------------------------------------------ 关账同成败落链


def test_close_writes_chain_projection(conn):
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    out = _close(conn)
    conn.commit()

    assert out["status"] == "closed"
    chain = conn.execute("SELECT * FROM occupancy_event_chain WHERE occupancy_id='OCC-1'").fetchone()
    assert chain is not None
    assert json.loads(chain["event_ids_json"]) == ["D1", "P1"]
    assert chain["drop_event_id"] == "D1"
    assert chain["pickup_event_id"] == "P1"
    assert chain["drop_event_ts"] == "2026-11-18T08:00:00Z"
    assert chain["pickup_event_ts"] == "2026-11-18T12:00:00Z"
    assert chain["event_count"] == 2
    bond = conn.execute("SELECT * FROM drop_pickup_bonds WHERE occupancy_id='OCC-1'").fetchone()
    assert chain["bond_id"] == bond["id"]
    # 关账回执双载：绑定边 + 事件链投影
    assert out["chain"]["event_ids"] == ["D1", "P1"]
    assert out["chain"]["bond_id"] == bond["id"]


def test_chain_failure_rolls_back_bond(conn):
    """链重算失败（窗内 pickup 腿被抽）→ 绑定边同滚，占用保持开放并搁置。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.commit()

    class ChainKiller:
        """代理连接：落链那一步抛 ChainError，其余透传。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *params):
            if str(sql).lstrip().upper().startswith("INSERT INTO OCCUPANCY_EVENT_CHAIN"):
                raise chain_proj.ChainError("chain_missing_pickup_leg", "人为抽掉 pickup 腿")
            return self._real.execute(sql, *params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    killer = ChainKiller(conn)
    occupancy = bond_proof.find_open_occupancy(killer, "CAB-A-01")
    with pytest.raises(chain_proj.ChainError):
        with bond_proof.atomic(killer, "bond_close"):
            bond_proof.close_with_bond(killer, occupancy, _pickup())

    assert conn.execute("SELECT COUNT(*) AS c FROM drop_pickup_bonds").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM occupancy_event_chain").fetchone()["c"] == 0
    assert conn.execute("SELECT status FROM occupancies WHERE id='OCC-1'").fetchone()[
        "status"
    ] == "occupied"
    assert conn.execute("SELECT status FROM cells WHERE id='CAB-A-01'").fetchone()[
        "status"
    ] == "occupied"


def test_materialize_chain_missing_pickup_leg_fails(conn):
    """人为抽掉 pickup 腿：事件不在库，materialize 必败。"""
    occ = conn.execute("SELECT * FROM occupancies WHERE id='OCC-1'").fetchone()
    drop = conn.execute("SELECT * FROM parcel_events WHERE id='D1'").fetchone()
    ghost_pickup = {
        "id": "P-GHOST",
        "event_type": "pickup",
        "event_ts": "2026-11-18T12:00:00Z",
        "tracking_no": "TN1",
        "cell_id": "CAB-A-01",
    }
    with pytest.raises(chain_proj.ChainError, match="缺 pickup 腿"):
        chain_proj.materialize_chain(conn, occ, "B-X", dict(drop), ghost_pickup)
    assert conn.execute("SELECT COUNT(*) AS c FROM occupancy_event_chain").fetchone()["c"] == 0


def test_delete_pickup_event_after_close_blocked(conn):
    """关账后人为抽 pickup 腿：外键（绑定边/链投影引用）必拒。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    out = _close(conn)
    assert out["status"] == "closed"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM parcel_events WHERE id='P1'")


def test_close_with_pickup_event_scrubbed_rejected(conn):
    """pickup 事件先入库又被抽走：关账拒绝，占用与格口不动。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.execute("DELETE FROM parcel_events WHERE id='P1'")
    out = _close(conn)
    conn.commit()
    assert out["status"] == "rejected"
    assert out["code"] == "pickup_not_found"
    assert conn.execute("SELECT COUNT(*) AS c FROM occupancy_event_chain").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM drop_pickup_bonds").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"] == 1


# ------------------------------------------------------------ 链序与时序


def test_inverted_leg_ts_rejected(conn):
    """drop 晚于 pickup：时序分叉，链拒落。"""
    _add_event(conn, "P0", "pickup", "CAB-A-01", "TN1", "2026-11-18T07:00:00Z")
    out = _close(conn, _pickup(event_id="P0", ts="2026-11-18T07:00:00Z"))
    assert out["status"] == "rejected"  # drop 侧解析不到早于 pickup 的 drop
    occ = conn.execute("SELECT * FROM occupancies WHERE id='OCC-1'").fetchone()
    drop = conn.execute("SELECT * FROM parcel_events WHERE id='D1'").fetchone()
    early_pickup = {
        "id": "P0",
        "event_type": "pickup",
        "event_ts": "2026-11-18T07:00:00Z",
        "tracking_no": "TN1",
        "cell_id": "CAB-A-01",
    }
    with pytest.raises(chain_proj.ChainError, match="时序分叉"):
        chain_proj.materialize_chain(conn, occ, "B-X", dict(drop), early_pickup)


def test_chain_tail_must_be_pickup(conn):
    """窗口内 pickup 之后还有同刻事件：链末不是 pickup，拒落。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    _add_event(conn, "ZZ", "drop", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    occ = conn.execute("SELECT * FROM occupancies WHERE id='OCC-1'").fetchone()
    drop = conn.execute("SELECT * FROM parcel_events WHERE id='D1'").fetchone()
    pickup = conn.execute("SELECT * FROM parcel_events WHERE id='P1'").fetchone()
    with pytest.raises(chain_proj.ChainError, match="链末"):
        chain_proj.materialize_chain(conn, occ, "B-X", dict(drop), dict(pickup))


# ------------------------------------------------------------ 库层兜底


def _close_ok(conn):
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    out = _close(conn)
    assert out["status"] == "closed"
    return out


def test_db_rejects_release_without_chain(conn):
    """有绑定边但无链投影，占用同样不得 released。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.execute(
        "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
        " pickup_event_id, tracking_no, created_at)"
        " VALUES ('B-RAW', 'OCC-1', 'D1', 'P1', 'TN1', '2026-11-18T12:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="事件链投影"):
        conn.execute("UPDATE occupancies SET status='released' WHERE id='OCC-1'")


def test_db_rejects_chain_endpoint_mismatch_with_bond(conn):
    """链端点与绑定边不一致：互证触发器必拒。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    _add_event(conn, "D2", "drop", "CAB-A-01", "TN1", "2026-11-18T09:00:00Z")
    conn.execute(
        "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
        " pickup_event_id, tracking_no, created_at)"
        " VALUES ('B-RAW', 'OCC-1', 'D1', 'P1', 'TN1', '2026-11-18T12:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="绑定边不一致"):
        conn.execute(
            "INSERT INTO occupancy_event_chain(occupancy_id, bond_id, event_ids_json,"
            " drop_event_id, pickup_event_id, drop_event_ts, pickup_event_ts,"
            " event_count, created_at)"
            " VALUES ('OCC-1', 'B-RAW', '[\"D2\", \"P1\"]', 'D2', 'P1',"
            " '2026-11-18T09:00:00Z', '2026-11-18T12:00:00Z', 2, '2026-11-18T12:00:00Z')"
        )


def test_db_rejects_chain_with_wrong_leg_ts(conn):
    """链落库的端点时序与事件库不一致：拒。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.execute(
        "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
        " pickup_event_id, tracking_no, created_at)"
        " VALUES ('B-RAW', 'OCC-1', 'D1', 'P1', 'TN1', '2026-11-18T12:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="pickup 端"):
        conn.execute(
            "INSERT INTO occupancy_event_chain(occupancy_id, bond_id, event_ids_json,"
            " drop_event_id, pickup_event_id, drop_event_ts, pickup_event_ts,"
            " event_count, created_at)"
            " VALUES ('OCC-1', 'B-RAW', '[\"D1\", \"P1\"]', 'D1', 'P1',"
            " '2026-11-18T08:00:00Z', '2026-11-18T13:00:00Z', 2, '2026-11-18T12:00:00Z')"
        )


def test_db_rejects_inverted_chain_ts(conn):
    """端点时序反序：CHECK 拒。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.execute(
        "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
        " pickup_event_id, tracking_no, created_at)"
        " VALUES ('B-RAW', 'OCC-1', 'D1', 'P1', 'TN1', '2026-11-18T12:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO occupancy_event_chain(occupancy_id, bond_id, event_ids_json,"
            " drop_event_id, pickup_event_id, drop_event_ts, pickup_event_ts,"
            " event_count, created_at)"
            " VALUES ('OCC-1', 'B-RAW', '[\"D1\", \"P1\"]', 'D1', 'P1',"
            " '2026-11-18T14:00:00Z', '2026-11-18T12:00:00Z', 2, '2026-11-18T12:00:00Z')"
        )


def test_chain_projection_immutable(conn):
    """投影只增：不可改、不可删。"""
    _close_ok(conn)
    with pytest.raises(sqlite3.IntegrityError, match="不可变"):
        conn.execute(
            "UPDATE occupancy_event_chain SET event_count=99 WHERE occupancy_id='OCC-1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="不可删除"):
        conn.execute("DELETE FROM occupancy_event_chain WHERE occupancy_id='OCC-1'")


# ------------------------------------------------------------ 核证：投影 / 绑定边 / 重放互证


def test_verify_chain_ok_after_full_drop_pickup(conn):
    _close_ok(conn)
    verdict = chain_proj.verify_chain(conn, "OCC-1")
    assert verdict["ok"] is True
    assert verdict["state"] == "verified"
    assert verdict["anomalies"] == []
    for key in (
        "bond_exists",
        "chain_exists",
        "endpoints_match_bond",
        "complete",
        "ordered",
        "endpoint_ts_match",
        "replay_match",
        "occupancy_ts_match",
    ):
        assert verdict["checks"][key] is True, key
    assert verdict["chain"]["event_ids"] == ["D1", "P1"]


def test_verify_chain_detects_replay_divergence(conn):
    """落链后窗内被人为补事件：重放与投影分叉，核证必报。"""
    _close_ok(conn)
    _add_event(conn, "X-MID", "drop", "CAB-A-01", "TN1", "2026-11-18T10:00:00Z")
    verdict = chain_proj.verify_chain(conn, "OCC-1")
    assert verdict["ok"] is False
    assert verdict["checks"]["replay_match"] is False
    assert any("重放不一致" in a for a in verdict["anomalies"])


def test_verify_open_occupancy_is_expected_state(conn):
    verdict = chain_proj.verify_chain(conn, "OCC-1")
    assert verdict["ok"] is True
    assert verdict["state"] == "open"
    assert verdict["expected"] == "closed_on_pickup"


def test_chain_proofs_list_covers_all_occupancies(conn):
    _close_ok(conn)
    _cell(conn, "CAB-A-02")
    _add_event(conn, "D2", "drop", "CAB-A-02", "TN2", "2026-11-18T09:00:00Z")
    _open_occupancy(conn, "OCC-2", "CAB-A-02", "TN2", "2026-11-18T09:00:00Z", "D2")
    conn.commit()

    rows = chain_proj.chain_proofs(conn)
    by_id = {r["occupancy_id"]: r for r in rows}
    assert by_id["OCC-1"]["ok"] is True
    assert by_id["OCC-1"]["chain"]["event_ids"] == ["D1", "P1"]
    assert by_id["OCC-1"]["anomaly"] is None
    assert by_id["OCC-2"]["state"] == "open"
    assert by_id["OCC-2"]["chain"] is None


# ------------------------------------------------------------ 存量补链


def test_backfill_chains_repairs_legacy(conn):
    """存量：已 released 有边无链 → 启动补链。"""
    _add_event(conn, "P1", "pickup", "CAB-A-01", "TN1", "2026-11-18T12:00:00Z")
    conn.execute("DROP TRIGGER trg_released_requires_bond")
    conn.execute("DROP TRIGGER trg_released_requires_chain")
    conn.execute(
        "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
        " pickup_event_id, tracking_no, source, created_at)"
        " VALUES ('B-LEG', 'OCC-1', 'D1', 'P1', 'TN1', 'close', '2026-11-18T12:00:00Z')"
    )
    conn.execute(
        "UPDATE occupancies SET status='released', closed_at='2026-11-18T12:00:00Z'"
        " WHERE id='OCC-1'"
    )
    conn.executescript(bond_proof.SCHEMA_SQL)
    conn.executescript(chain_proj.SCHEMA_SQL)

    report = chain_proj.backfill_chains(conn)
    assert report["backfilled"] == 1
    chain = conn.execute("SELECT * FROM occupancy_event_chain WHERE occupancy_id='OCC-1'").fetchone()
    assert json.loads(chain["event_ids_json"]) == ["D1", "P1"]
    assert chain["bond_id"] == "B-LEG"
    # 幂等
    assert chain_proj.backfill_chains(conn)["backfilled"] == 0


def test_backfill_chains_quarantines_unrepairable(tmp_path, monkeypatch):
    """补链重算失败（链末不是 pickup）→ 写搁置，不硬落。"""
    c = _connect(tmp_path, monkeypatch)
    _cell(c, "CAB-A-02")
    _add_event(c, "D2", "drop", "CAB-A-02", "TN2", "2026-11-18T10:00:00Z")
    _add_event(c, "P2", "pickup", "CAB-A-02", "TN2", "2026-11-18T11:00:00Z")
    _add_event(c, "ZZ", "drop", "CAB-A-02", "TN2", "2026-11-18T11:00:00Z")
    c.execute("DROP TRIGGER trg_occupancy_insert_not_released")
    c.execute(
        "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status)"
        " VALUES ('OCC-2', 'CAB-A-02', 'TN2', '2026-11-18T10:00:00Z',"
        " '2026-11-18T11:00:00Z', 'released')"
    )
    c.execute(
        "INSERT INTO drop_pickup_bonds(id, occupancy_id, drop_event_id,"
        " pickup_event_id, tracking_no, source, created_at)"
        " VALUES ('B-LEG2', 'OCC-2', 'D2', 'P2', 'TN2', 'backfill', '2026-11-18T11:00:00Z')"
    )
    c.executescript(bond_proof.SCHEMA_SQL)
    c.commit()

    report = chain_proj.backfill_chains(c)
    assert report["backfilled"] == 0
    assert report["quarantined"] == 1
    q = c.execute("SELECT * FROM quarantine").fetchone()
    assert q["reason"] == chain_proj.REASON_BACKFILL
    assert c.execute("SELECT COUNT(*) AS c FROM occupancy_event_chain").fetchone()["c"] == 0
    c.close()


# ------------------------------------------------------------ 只读接口（格阵下钻 / 核证）


def test_chain_for_returns_full_ordered_projection(conn):
    _close_ok(conn)
    chain = chain_proj.chain_for(conn, "OCC-1")
    assert chain["complete"] is True
    assert chain["ordered"] is True
    assert chain["event_ids"] == ["D1", "P1"]
    assert [e["event_type"] for e in chain["events"]] == ["drop", "pickup"]
    assert chain["drop_event_ts"] == "2026-11-18T08:00:00Z"
    assert chain["pickup_event_ts"] == "2026-11-18T12:00:00Z"
    assert chain["source"] == "projection"


def test_chain_for_open_occupancy_live_replay(conn):
    chain = chain_proj.chain_for(conn, "OCC-1")
    assert chain["complete"] is False
    assert chain["event_ids"] == ["D1"]
    assert chain["expected"] == "closed_on_pickup"


def test_chain_for_unknown_occupancy(conn):
    assert chain_proj.chain_for(conn, "OCC-NOPE") is None
    assert chain_proj.verify_chain(conn, "OCC-NOPE") is None


# ------------------------------------------------------------ HTTP 端到端


def _http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lockerbay.db")
    fixtures = Path(__file__).resolve().parents[2] / "fixtures"
    monkeypatch.setattr(db_mod, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(seed_mod, "FIXTURES_DIR", fixtures)
    from app.main import app

    return TestClient(app)


def test_http_chain_endpoints(tmp_path, monkeypatch):
    with _http_client(tmp_path, monkeypatch) as client:
        # 完整 drop+pickup 的占用：链两端齐全且有序
        chain = client.get("/api/occupancy-chain/OCC-CAB-A-01-E1")
        assert chain.status_code == 200
        body = chain.json()
        assert body["complete"] is True
        assert body["ordered"] is True
        assert body["event_ids"] == ["E1", "E3"]
        assert [e["event_type"] for e in body["events"]] == ["drop", "pickup"]
        assert body["drop_event_ts"] == "2026-11-18T08:00:00Z"
        assert body["pickup_event_ts"] == "2026-11-18T12:00:00Z"

        verdict = client.get("/api/occupancy-chain/OCC-CAB-A-01-E1/verify")
        assert verdict.status_code == 200
        v = verdict.json()
        assert v["ok"] is True
        assert v["checks"]["endpoints_match_bond"] is True
        assert v["checks"]["replay_match"] is True

        # 开放占用：实时重放，待取件
        proofs = client.get("/api/chain-proofs").json()
        open_row = [r for r in proofs if r["status"] == "occupied"][0]
        live = client.get(f"/api/occupancy-chain/{open_row['occupancy_id']}").json()
        assert live["complete"] is False
        assert live["expected"] == "closed_on_pickup"

        closed_row = [r for r in proofs if r["occupancy_id"] == "OCC-CAB-A-01-E1"][0]
        assert closed_row["ok"] is True
        assert closed_row["chain"]["event_ids"] == ["E1", "E3"]

        assert client.get("/api/occupancy-chain/OCC-NOPE").status_code == 404
        assert client.get("/api/occupancy-chain/OCC-NOPE/verify").status_code == 404


def test_http_ingest_receipt_carries_chain(tmp_path, monkeypatch):
    with _http_client(tmp_path, monkeypatch) as client:
        report = client.post("/api/ingest/run").json()
        assert report["bonded"] == 1
        assert report["chained"] == 1
        receipt = report["close_receipts"][0]
        assert receipt["occupancy_id"] == "OCC-CAB-A-03-E5"
        assert receipt["chain_event_ids"] == ["E5", "E6"]
        assert receipt["chain_drop_ts"] == "2026-11-19T02:00:00Z"
        assert receipt["chain_pickup_ts"] == "2026-11-19T06:15:00Z"
        assert receipt["chain_event_count"] == 2

        # 装载关账后链投影落库，核证通过
        verdict = client.get("/api/occupancy-chain/OCC-CAB-A-03-E5/verify").json()
        assert verdict["ok"] is True
        assert verdict["chain"]["event_ids"] == ["E5", "E6"]
