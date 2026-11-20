"""占用事件链投影：关账绑定标成功必须同成败落一条有序事件链。

形态选型（与需求一致，各择一）：
  - 落库：独立 ``occupancy_event_chain`` 表（占用行不挂 chain_json 快照列）；
  - 提交：写绑定边同事务重算并落事件链，缺一回滚（不做链草稿后原子切换）。

链 = 占用窗口 [drop_ts, pickup_ts] 内同格口同运单事件按 (event_ts, id) 的重放序列，
首腿 drop、末腿 pickup，两端点时序随链落库；投影与绑定边互证并进入占用对账。

库层触发器兜底：
  ``trg_chain_matches_bond``       链端点必须等于绑定边两端（互证）；
  ``trg_chain_legs_valid``         端点事件存在、类型正确、时序与事件库一致；
  ``trg_released_requires_chain``  占用无事件链投影不得 UPDATE 成 released；
  ``trg_chain_no_update/delete``   投影只增不改不删（不可变快照语义）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

STATUS = "implemented"

REASON_BACKFILL = "存量释放无法补事件链投影"

EVENT_FIELDS = ("id", "cabinet_id", "cell_id", "event_type", "event_ts", "tracking_no")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS occupancy_event_chain (
  occupancy_id TEXT PRIMARY KEY REFERENCES occupancies(id),
  bond_id TEXT NOT NULL UNIQUE REFERENCES drop_pickup_bonds(id),
  event_ids_json TEXT NOT NULL,
  drop_event_id TEXT NOT NULL REFERENCES parcel_events(id),
  pickup_event_id TEXT NOT NULL REFERENCES parcel_events(id),
  drop_event_ts TEXT NOT NULL,
  pickup_event_ts TEXT NOT NULL,
  event_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (drop_event_id <> pickup_event_id),
  CHECK (drop_event_ts <= pickup_event_ts),
  CHECK (event_count >= 2)
);

CREATE INDEX IF NOT EXISTS ix_chain_bond ON occupancy_event_chain(bond_id);

CREATE TRIGGER IF NOT EXISTS trg_chain_matches_bond
BEFORE INSERT ON occupancy_event_chain
BEGIN
  SELECT RAISE(ABORT, '事件链端点与绑定边不一致')
   WHERE NOT EXISTS (
     SELECT 1 FROM drop_pickup_bonds b
      WHERE b.id = NEW.bond_id
        AND b.occupancy_id = NEW.occupancy_id
        AND b.drop_event_id = NEW.drop_event_id
        AND b.pickup_event_id = NEW.pickup_event_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_chain_legs_valid
BEFORE INSERT ON occupancy_event_chain
BEGIN
  SELECT RAISE(ABORT, '链 drop 端事件不存在或类型/时序不符')
   WHERE NOT EXISTS (
     SELECT 1 FROM parcel_events e
      WHERE e.id = NEW.drop_event_id
        AND e.event_type = 'drop'
        AND e.event_ts = NEW.drop_event_ts);
  SELECT RAISE(ABORT, '链 pickup 端事件不存在或类型/时序不符')
   WHERE NOT EXISTS (
     SELECT 1 FROM parcel_events e
      WHERE e.id = NEW.pickup_event_id
        AND e.event_type = 'pickup'
        AND e.event_ts = NEW.pickup_event_ts);
END;

CREATE TRIGGER IF NOT EXISTS trg_released_requires_chain
BEFORE UPDATE OF status ON occupancies
WHEN NEW.status = 'released'
 AND EXISTS (
   SELECT 1 FROM drop_pickup_bonds b WHERE b.occupancy_id = NEW.id)
 AND NOT EXISTS (
   SELECT 1 FROM occupancy_event_chain c WHERE c.occupancy_id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, '占用无事件链投影不得释放');
END;

CREATE TRIGGER IF NOT EXISTS trg_chain_no_update
BEFORE UPDATE ON occupancy_event_chain
BEGIN
  SELECT RAISE(ABORT, '事件链投影不可变');
END;

CREATE TRIGGER IF NOT EXISTS trg_chain_no_delete
BEFORE DELETE ON occupancy_event_chain
BEGIN
  SELECT RAISE(ABORT, '事件链投影不可删除');
END;
"""


class ChainError(Exception):
    """链投影落库前校验失败（尚未改库，可由调用方转搁置）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@contextmanager
def atomic(conn, name: str = "chain"):
    """SAVEPOINT 级原子段：外层事务由调用方 commit。"""
    sp = f"sp_{name}"
    conn.execute(f"SAVEPOINT {sp}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {sp}")
        conn.execute(f"RELEASE {sp}")
        raise
    else:
        conn.execute(f"RELEASE {sp}")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_view(row) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in EVENT_FIELDS if k in row.keys()}


# ---------------------------------------------------------------- 事件扫描 / 链重算


def scan_window(conn, cell_id: str, tracking_no: str, start_ts: str, end_ts: str) -> list[dict]:
    """事件扫描：占用窗口内同格口同运单事件，按 (event_ts, id) 时序重放。"""
    rows = conn.execute(
        """
        SELECT * FROM parcel_events
         WHERE cell_id=? AND tracking_no=? AND event_ts>=? AND event_ts<=?
         ORDER BY event_ts, id
        """,
        (cell_id, tracking_no, start_ts, end_ts),
    ).fetchall()
    return [dict(r) for r in rows]


def build_chain(conn, occupancy, drop_event: dict, pickup_event: dict) -> list[dict]:
    """重算占用事件链：双腿必须在窗、首 drop 末 pickup、链序不与时序分叉。"""
    drop_ts = drop_event["event_ts"]
    pickup_ts = pickup_event["event_ts"]
    if drop_ts > pickup_ts:
        raise ChainError(
            "chain_ts_inverted",
            f"链端点时序分叉：drop {drop_event['id']}@{drop_ts} 晚于 "
            f"pickup {pickup_event['id']}@{pickup_ts}，拒绝落链",
        )
    events = scan_window(
        conn, occupancy["cell_id"], pickup_event["tracking_no"], drop_ts, pickup_ts
    )
    ids = [e["id"] for e in events]
    if drop_event["id"] not in ids:
        raise ChainError(
            "chain_missing_drop_leg",
            f"事件链缺 drop 腿 {drop_event['id']}：窗口内扫描不到，拒绝落链",
        )
    if pickup_event["id"] not in ids:
        raise ChainError(
            "chain_missing_pickup_leg",
            f"事件链缺 pickup 腿 {pickup_event['id']}：窗口内扫描不到，拒绝落链",
        )
    if ids[0] != drop_event["id"]:
        raise ChainError(
            "chain_head_not_drop",
            f"链首 {ids[0]} 不是 drop 腿 {drop_event['id']}，链序与时序分叉，拒绝落链",
        )
    if ids[-1] != pickup_event["id"]:
        raise ChainError(
            "chain_tail_not_pickup",
            f"链末 {ids[-1]} 不是 pickup 腿 {pickup_event['id']}，链序与时序分叉，拒绝落链",
        )
    for prev, cur in zip(events, events[1:]):
        if prev["event_ts"] > cur["event_ts"]:
            raise ChainError(
                "chain_order_diverged",
                f"链序与事件时序分叉：{prev['id']}@{prev['event_ts']} 排在 "
                f"{cur['id']}@{cur['event_ts']} 之前，拒绝落链",
            )
    return events


def materialize_chain(
    conn, occupancy, bond_id: str, drop_event: dict, pickup_event: dict, now: str | None = None
) -> dict:
    """重算并落事件链投影。调用方负责事务边界（与绑定边同一 atomic 段）。"""
    now = now or _utcnow()
    for ev, etype in ((drop_event, "drop"), (pickup_event, "pickup")):
        row = conn.execute("SELECT * FROM parcel_events WHERE id=?", (ev["id"],)).fetchone()
        if row is None:
            raise ChainError(
                f"chain_missing_{etype}_leg",
                f"事件链缺 {etype} 腿 {ev['id']}：事件不在库，拒绝落链",
            )
        if row["event_type"] != etype:
            raise ChainError(
                f"chain_{etype}_type_invalid",
                f"链 {etype} 端 {ev['id']} 实际类型是 {row['event_type']}，拒绝落链",
            )
    events = build_chain(conn, occupancy, drop_event, pickup_event)
    ids = [e["id"] for e in events]
    conn.execute(
        "INSERT INTO occupancy_event_chain"
        "(occupancy_id, bond_id, event_ids_json, drop_event_id, pickup_event_id,"
        " drop_event_ts, pickup_event_ts, event_count, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            occupancy["id"],
            bond_id,
            json.dumps(ids, ensure_ascii=False),
            drop_event["id"],
            pickup_event["id"],
            drop_event["event_ts"],
            pickup_event["event_ts"],
            len(ids),
            now,
        ),
    )
    return {
        "occupancy_id": occupancy["id"],
        "bond_id": bond_id,
        "event_ids": ids,
        "drop_event_id": drop_event["id"],
        "pickup_event_id": pickup_event["id"],
        "drop_event_ts": drop_event["event_ts"],
        "pickup_event_ts": pickup_event["event_ts"],
        "event_count": len(ids),
    }


# ---------------------------------------------------------------- 存量迁移


def _quarantine(conn, reason: str, member: dict, now: str) -> bool:
    """写搁置；同 reason + 同成员只留一条。返回是否新写入。"""
    member_json = json.dumps(member, ensure_ascii=False, sort_keys=True)
    exists = conn.execute(
        "SELECT 1 FROM quarantine WHERE reason=? AND member_json=?",
        (reason, member_json),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO quarantine(reason, member_json, created_at) VALUES (?,?,?)",
        (reason, member_json, now),
    )
    return True


def backfill_chains(conn) -> dict:
    """给存量「已 released 有绑定边但无链投影」的占用补链；补不齐写搁置。幂等。"""
    now = _utcnow()
    rows = conn.execute(
        """
        SELECT o.*, b.id AS bond_id, b.drop_event_id, b.pickup_event_id
          FROM occupancies o
          JOIN drop_pickup_bonds b ON b.occupancy_id = o.id
          LEFT JOIN occupancy_event_chain c ON c.occupancy_id = o.id
         WHERE o.status='released' AND c.occupancy_id IS NULL
         ORDER BY o.opened_at
        """
    ).fetchall()

    report = {"backfilled": 0, "quarantined": 0, "occupancy_ids": []}
    for occ in rows:
        drop = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (occ["drop_event_id"],)
        ).fetchone()
        pickup = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (occ["pickup_event_id"],)
        ).fetchone()
        if drop is None or pickup is None:
            _quarantine(
                conn,
                REASON_BACKFILL,
                {
                    "code": "backfill_missing_leg",
                    "occupancy_id": occ["id"],
                    "bond_id": occ["bond_id"],
                    "drop_event_id": occ["drop_event_id"],
                    "pickup_event_id": occ["pickup_event_id"],
                },
                now,
            )
            report["quarantined"] += 1
            continue
        try:
            with atomic(conn, "chain_backfill"):
                materialize_chain(conn, occ, occ["bond_id"], dict(drop), dict(pickup), now)
        except (ChainError, sqlite3.IntegrityError) as exc:
            _quarantine(
                conn,
                REASON_BACKFILL,
                {
                    "code": "backfill_chain_failed",
                    "occupancy_id": occ["id"],
                    "bond_id": occ["bond_id"],
                    "detail": str(exc),
                },
                now,
            )
            report["quarantined"] += 1
            continue
        report["backfilled"] += 1
        report["occupancy_ids"].append(occ["id"])
    return report


# ---------------------------------------------------------------- 只读：格阵下钻 / 核证对账


def _chain_row(conn, occupancy_id: str):
    return conn.execute(
        "SELECT * FROM occupancy_event_chain WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()


def _hydrate(conn, event_ids: list[str]) -> list[dict]:
    out = []
    for eid in event_ids:
        row = conn.execute("SELECT * FROM parcel_events WHERE id=?", (eid,)).fetchone()
        if row is None:
            out.append({"id": eid, "missing": True})
        else:
            out.append(_event_view(row))
    return out


def _chain_summary(row) -> dict:
    return {
        "occupancy_id": row["occupancy_id"],
        "bond_id": row["bond_id"],
        "event_ids": json.loads(row["event_ids_json"]),
        "drop_event_id": row["drop_event_id"],
        "pickup_event_id": row["pickup_event_id"],
        "drop_event_ts": row["drop_event_ts"],
        "pickup_event_ts": row["pickup_event_ts"],
        "event_count": row["event_count"],
        "created_at": row["created_at"],
    }


def chain_for(conn, occupancy_id: str) -> dict | None:
    """格阵下钻数据源：已释放给持久投影，开放占用给实时重放。占用不存在返回 None。"""
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if occ is None:
        return None
    chain = _chain_row(conn, occupancy_id)
    if chain is not None:
        ids = json.loads(chain["event_ids_json"])
        return {
            "occupancy_id": occupancy_id,
            "status": occ["status"],
            "bond_id": chain["bond_id"],
            "event_ids": ids,
            "events": _hydrate(conn, ids),
            "drop_event_id": chain["drop_event_id"],
            "pickup_event_id": chain["pickup_event_id"],
            "drop_event_ts": chain["drop_event_ts"],
            "pickup_event_ts": chain["pickup_event_ts"],
            "event_count": chain["event_count"],
            "ordered": True,
            "complete": True,
            "source": "projection",
        }
    if occ["status"] == "occupied":
        events = scan_window(
            conn, occ["cell_id"], occ["tracking_no"], occ["opened_at"], "9999-12-31T23:59:59Z"
        )
        ids = [e["id"] for e in events]
        return {
            "occupancy_id": occupancy_id,
            "status": "occupied",
            "bond_id": None,
            "event_ids": ids,
            "events": [_event_view(e) for e in events],
            "drop_event_id": ids[0] if ids else None,
            "pickup_event_id": None,
            "drop_event_ts": events[0]["event_ts"] if events else None,
            "pickup_event_ts": None,
            "event_count": len(ids),
            "ordered": True,
            "complete": False,
            "source": "live",
            "expected": "closed_on_pickup",
        }
    # 已释放但缺投影（启动补链应已覆盖；此处兜底重放并标异常，绝不可静默装齐）。
    bond = conn.execute(
        "SELECT * FROM drop_pickup_bonds WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    ids: list[str] = []
    events: list[dict] = []
    if bond is not None:
        drop = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (bond["drop_event_id"],)
        ).fetchone()
        pickup = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (bond["pickup_event_id"],)
        ).fetchone()
        if drop is not None and pickup is not None:
            events = scan_window(
                conn, occ["cell_id"], occ["tracking_no"], drop["event_ts"], pickup["event_ts"]
            )
            ids = [e["id"] for e in events]
    return {
        "occupancy_id": occupancy_id,
        "status": occ["status"],
        "bond_id": bond["id"] if bond is not None else None,
        "event_ids": ids,
        "events": [_event_view(e) for e in events],
        "drop_event_id": bond["drop_event_id"] if bond is not None else None,
        "pickup_event_id": bond["pickup_event_id"] if bond is not None else None,
        "drop_event_ts": events[0]["event_ts"] if events else None,
        "pickup_event_ts": events[-1]["event_ts"] if events else None,
        "event_count": len(ids),
        "ordered": True,
        "complete": False,
        "source": "replay",
        "projection_missing": True,
        "anomaly": "已释放但缺事件链投影",
    }


def verify_chain(conn, occupancy_id: str) -> dict | None:
    """事件链核证：重放事件库，与持久投影、绑定边三方互证。占用不存在返回 None。"""
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if occ is None:
        return None
    bond = conn.execute(
        "SELECT * FROM drop_pickup_bonds WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    chain = _chain_row(conn, occupancy_id)

    checks: dict[str, bool] = {
        "bond_exists": bond is not None,
        "chain_exists": chain is not None,
    }
    anomalies: list[str] = []

    if occ["status"] == "occupied" and bond is None and chain is None:
        return {
            "occupancy_id": occupancy_id,
            "status": occ["status"],
            "bond_id": None,
            "chain": None,
            "checks": checks,
            "anomalies": [],
            "ok": True,
            "state": "open",
            "expected": "closed_on_pickup",
        }
    if occ["status"] == "occupied" and (bond is not None or chain is not None):
        anomalies.append("占用未释放但已存在绑定边/事件链投影")
    if occ["status"] == "released":
        if bond is None:
            anomalies.append("已释放但缺绑定边")
        if chain is None:
            anomalies.append("已释放但缺事件链投影")

    chain_summary = None
    if chain is not None:
        chain_summary = _chain_summary(chain)
        ids = chain_summary["event_ids"]
        if bond is not None:
            match = (
                chain["drop_event_id"] == bond["drop_event_id"]
                and chain["pickup_event_id"] == bond["pickup_event_id"]
            )
            checks["endpoints_match_bond"] = match
            if not match:
                anomalies.append("事件链端点与绑定边不一致")
        complete = (
            len(ids) >= 2
            and ids[0] == chain["drop_event_id"]
            and ids[-1] == chain["pickup_event_id"]
        )
        checks["complete"] = complete
        if not complete:
            anomalies.append("事件链缺腿：首末两端不是 drop/pickup")
        events = _hydrate(conn, ids)
        tss = [e.get("event_ts") for e in events]
        ordered = all(tss) and all(a <= b for a, b in zip(tss, tss[1:]))
        checks["ordered"] = bool(ordered)
        if not ordered:
            anomalies.append("链序与事件时序分叉")
        drop_ev = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (chain["drop_event_id"],)
        ).fetchone()
        pickup_ev = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (chain["pickup_event_id"],)
        ).fetchone()
        ts_match = (
            drop_ev is not None
            and pickup_ev is not None
            and drop_ev["event_ts"] == chain["drop_event_ts"]
            and pickup_ev["event_ts"] == chain["pickup_event_ts"]
            and chain["drop_event_ts"] <= chain["pickup_event_ts"]
        )
        checks["endpoint_ts_match"] = bool(ts_match)
        if not ts_match:
            anomalies.append("链端点时序与事件库不一致")
        if drop_ev is not None and pickup_ev is not None:
            replay = [
                e["id"]
                for e in scan_window(
                    conn,
                    occ["cell_id"],
                    occ["tracking_no"],
                    drop_ev["event_ts"],
                    pickup_ev["event_ts"],
                )
            ]
            checks["replay_match"] = replay == ids
            if replay != ids:
                anomalies.append("事件链投影与事件库重放不一致")
        if occ["status"] == "released":
            occ_ts = (
                occ["opened_at"] == chain["drop_event_ts"]
                and occ["closed_at"] == chain["pickup_event_ts"]
            )
            checks["occupancy_ts_match"] = occ_ts
            if not occ_ts:
                anomalies.append("占用账时序与链端点不一致")

    ok = not anomalies and all(checks.values())
    return {
        "occupancy_id": occupancy_id,
        "status": occ["status"],
        "bond_id": bond["id"] if bond is not None else None,
        "chain": chain_summary,
        "checks": checks,
        "anomalies": anomalies,
        "ok": ok,
        "state": "verified" if ok else "anomaly",
    }


def chain_proofs(conn, limit: int = 200) -> list[dict]:
    """事件链对账：每个占用一行，投影/绑定边/事件库重放互证结果。"""
    rows = conn.execute(
        """
        SELECT o.id AS occupancy_id, o.cell_id, o.tracking_no, o.opened_at,
               o.closed_at, o.status,
               b.id AS bond_id
          FROM occupancies o
          LEFT JOIN drop_pickup_bonds b ON b.occupancy_id = o.id
         ORDER BY o.opened_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()

    out = []
    for row in rows:
        verdict = verify_chain(conn, row["occupancy_id"])
        chain = _chain_row(conn, row["occupancy_id"])
        out.append(
            {
                "occupancy_id": row["occupancy_id"],
                "cell_id": row["cell_id"],
                "tracking_no": row["tracking_no"],
                "status": row["status"],
                "opened_at": row["opened_at"],
                "closed_at": row["closed_at"],
                "bond_id": row["bond_id"],
                "chain": _chain_summary(chain) if chain is not None else None,
                "checks": verdict["checks"],
                "ok": verdict["ok"],
                "state": verdict["state"],
                "anomaly": verdict["anomalies"][0] if verdict["anomalies"] else None,
            }
        )
    return out
