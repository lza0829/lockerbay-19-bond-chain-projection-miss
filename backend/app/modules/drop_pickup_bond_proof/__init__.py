"""投取事件绑定核证：关占用必须物化一条 drop↔pickup 绑定边。

形态选型（与需求一致，择一）：
  - 独立 bond 边表 ``drop_pickup_bonds``；
  - 关账失败闭：同事务写边 + 关占用 + 改格口；
  - 缺 drop 侧：拒关账并写 ``quarantine``；
  - 存量已释放无边：启动补边，补不齐写搁置。

库层另有两条触发器兜底：
  ``trg_bond_legs_valid``            绑定边两端事件必须存在、类型正确、运单一致；
  ``trg_released_requires_bond``     占用无绑定边不得 UPDATE 成 released。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

STATUS = "implemented"

CLOSE_SOURCE = "close"
BACKFILL_SOURCE = "backfill"

REASON_NO_OPEN = "格口无开放占用，拒绝关账"
REASON_MISSING_DROP = "pickup 无可绑定的 drop 侧，拒绝关账"
REASON_BACKFILL = "存量释放无法补边"

EVENT_FIELDS = ("id", "cabinet_id", "cell_id", "event_type", "event_ts", "tracking_no")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS drop_pickup_bonds (
  id TEXT PRIMARY KEY,
  occupancy_id TEXT NOT NULL UNIQUE REFERENCES occupancies(id),
  drop_event_id TEXT NOT NULL REFERENCES parcel_events(id),
  pickup_event_id TEXT NOT NULL REFERENCES parcel_events(id),
  tracking_no TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'close',
  created_at TEXT NOT NULL,
  CHECK (drop_event_id <> pickup_event_id)
);

CREATE INDEX IF NOT EXISTS ix_bond_drop ON drop_pickup_bonds(drop_event_id);
CREATE INDEX IF NOT EXISTS ix_bond_pickup ON drop_pickup_bonds(pickup_event_id);

CREATE TRIGGER IF NOT EXISTS trg_bond_legs_valid
BEFORE INSERT ON drop_pickup_bonds
BEGIN
  SELECT RAISE(ABORT, 'drop 端事件不存在或类型不符')
   WHERE NOT EXISTS (
     SELECT 1 FROM parcel_events e
      WHERE e.id = NEW.drop_event_id AND e.event_type = 'drop');
  SELECT RAISE(ABORT, 'pickup 端事件不存在或类型不符')
   WHERE NOT EXISTS (
     SELECT 1 FROM parcel_events e
      WHERE e.id = NEW.pickup_event_id AND e.event_type = 'pickup');
  SELECT RAISE(ABORT, '绑定边运单与两端事件不一致')
   WHERE NOT EXISTS (
     SELECT 1
       FROM parcel_events d
       JOIN parcel_events p ON p.id = NEW.pickup_event_id
      WHERE d.id = NEW.drop_event_id
        AND d.tracking_no = NEW.tracking_no
        AND p.tracking_no = NEW.tracking_no);
END;

CREATE TRIGGER IF NOT EXISTS trg_released_requires_bond
BEFORE UPDATE OF status ON occupancies
WHEN NEW.status = 'released'
 AND NOT EXISTS (
   SELECT 1 FROM drop_pickup_bonds b WHERE b.occupancy_id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, '占用无绑定边不得释放');
END;

CREATE TRIGGER IF NOT EXISTS trg_occupancy_insert_not_released
BEFORE INSERT ON occupancies
WHEN NEW.status = 'released'
BEGIN
  SELECT RAISE(ABORT, '占用不得以 released 直接建账，需取件关账写绑定边');
END;
"""


class BondError(Exception):
    """关账前校验失败（尚未改库，可直接转搁置）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@contextmanager
def atomic(conn, name: str = "bond"):
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


# ---------------------------------------------------------------- 关账写路径


def find_open_occupancy(conn, cell_id):
    return conn.execute(
        "SELECT * FROM occupancies WHERE cell_id=? AND status='occupied' "
        "ORDER BY opened_at DESC LIMIT 1",
        (cell_id,),
    ).fetchone()


def validate_pickup(conn, pickup_event) -> dict:
    event_id = pickup_event.get("id")
    row = conn.execute("SELECT * FROM parcel_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise BondError("pickup_not_found", f"pickup 事件 {event_id} 不存在，拒绝关账")
    if row["event_type"] != "pickup":
        raise BondError("pickup_type_invalid", f"事件 {event_id} 不是 pickup，拒绝关账")
    if not row["tracking_no"]:
        raise BondError("missing_tracking", f"pickup 事件 {event_id} 缺运单，无法绑定")
    return dict(row)


def resolve_drop_leg(conn, occupancy, pickup_event) -> dict:
    """按占用找 pickup 对应的 drop 端：占用自身事件优先，其次同格口同运单最近 drop。"""
    tracking = pickup_event.get("tracking_no")
    cell_id = pickup_event.get("cell_id") or occupancy["cell_id"]
    pickup_ts = pickup_event.get("event_ts")

    last_id = occupancy["last_event_id"]
    if last_id:
        row = conn.execute("SELECT * FROM parcel_events WHERE id=?", (last_id,)).fetchone()
        if (
            row is not None
            and row["event_type"] == "drop"
            and row["tracking_no"] == tracking
            and row["cell_id"] == cell_id
            and row["event_ts"] <= pickup_ts
        ):
            return dict(row)

    row = conn.execute(
        """
        SELECT * FROM parcel_events
         WHERE event_type='drop' AND cell_id=? AND tracking_no=?
           AND event_ts <= ?
         ORDER BY event_ts DESC, id DESC
         LIMIT 1
        """,
        (cell_id, tracking, pickup_ts),
    ).fetchone()
    if row is not None:
        return dict(row)

    raise BondError(
        "missing_drop_leg",
        f"pickup {pickup_event.get('id')} 在格口 {cell_id} / 运单 {tracking} 下无 drop 可核对，拒绝关账",
    )


def insert_bond(
    conn,
    occupancy_id: str,
    drop_event: dict,
    pickup_event: dict,
    now: str,
    source: str = CLOSE_SOURCE,
) -> str:
    bond_id = f"BND-{uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO drop_pickup_bonds"
        "(id, occupancy_id, drop_event_id, pickup_event_id, tracking_no, source, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            bond_id,
            occupancy_id,
            drop_event["id"],
            pickup_event["id"],
            pickup_event["tracking_no"],
            source,
            now,
        ),
    )
    return bond_id


def close_with_bond(conn, occupancy, pickup_event, now: str | None = None) -> dict:
    """写边 → 关占用 → 改格口。调用方负责事务边界（外层 atomic / commit）。"""
    now = now or _utcnow()
    pickup = validate_pickup(conn, pickup_event)
    drop = resolve_drop_leg(conn, occupancy, pickup)
    bond_id = insert_bond(conn, occupancy["id"], drop, pickup, now, CLOSE_SOURCE)

    cur = conn.execute(
        "UPDATE occupancies SET closed_at=?, status='released', last_event_id=? "
        "WHERE id=? AND status='occupied'",
        (pickup["event_ts"], pickup["id"], occupancy["id"]),
    )
    if cur.rowcount != 1:
        raise BondError(
            "occupancy_not_open", f"占用 {occupancy['id']} 不是开放态，拒绝关账"
        )
    conn.execute("UPDATE cells SET status='free' WHERE id=?", (occupancy["cell_id"],))
    return {
        "bond_id": bond_id,
        "occupancy_id": occupancy["id"],
        "drop_event_id": drop["id"],
        "pickup_event_id": pickup["id"],
        "tracking_no": pickup["tracking_no"],
        "cell_id": occupancy["cell_id"],
    }


def try_close_pickup(conn, pickup_event, now: str | None = None) -> dict:
    """装载 / 播种共用的取件关账入口：成功返回 closed，失败写搁置后返回 rejected。"""
    now = now or _utcnow()
    cell_id = pickup_event.get("cell_id")
    if not cell_id:
        return _reject(conn, pickup_event, "missing_cell", "pickup 缺格口，拒绝关账", now)

    occupancy = find_open_occupancy(conn, cell_id)
    if occupancy is None:
        return _reject(conn, pickup_event, "no_open_occupancy", REASON_NO_OPEN, now)

    try:
        with atomic(conn, "bond_close"):
            result = close_with_bond(conn, occupancy, pickup_event, now)
    except BondError as exc:
        return _reject(
            conn,
            pickup_event,
            exc.code,
            REASON_MISSING_DROP if exc.code == "missing_drop_leg" else exc.message,
            now,
            detail=exc.message if exc.code == "missing_drop_leg" else None,
        )
    except sqlite3.IntegrityError as exc:
        return _reject(conn, pickup_event, "bond_integrity", str(exc), now)
    return {"status": "closed", **result}


def _reject(conn, pickup_event, code: str, reason: str, now: str, detail=None) -> dict:
    member = {
        "code": code,
        "pickup_event_id": pickup_event.get("id"),
        "cell_id": pickup_event.get("cell_id"),
        "tracking_no": pickup_event.get("tracking_no"),
    }
    if detail:
        member["detail"] = detail
    _quarantine(conn, reason, member, now)
    return {
        "status": "rejected",
        "code": code,
        "reason": reason,
        "detail": detail,
        "pickup_event_id": pickup_event.get("id"),
        "cell_id": pickup_event.get("cell_id"),
        "tracking_no": pickup_event.get("tracking_no"),
    }


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


# ---------------------------------------------------------------- 存量迁移


def _find_pickup_for(conn, occupancy):
    last_id = occupancy["last_event_id"]
    if last_id:
        row = conn.execute("SELECT * FROM parcel_events WHERE id=?", (last_id,)).fetchone()
        if (
            row is not None
            and row["event_type"] == "pickup"
            and row["tracking_no"] == occupancy["tracking_no"]
            and row["cell_id"] == occupancy["cell_id"]
        ):
            return dict(row)
    closed_at = occupancy["closed_at"] or "9999-12-31T23:59:59Z"
    row = conn.execute(
        """
        SELECT * FROM parcel_events
         WHERE event_type='pickup' AND cell_id=? AND tracking_no=?
           AND event_ts >= ? AND event_ts <= ?
         ORDER BY event_ts DESC, id DESC
         LIMIT 1
        """,
        (
            occupancy["cell_id"],
            occupancy["tracking_no"],
            occupancy["opened_at"],
            closed_at,
        ),
    ).fetchone()
    return dict(row) if row is not None else None


def backfill_bonds(conn) -> dict:
    """给存量「已 released 但无绑定边」的占用补边；补不齐写搁置。幂等。"""
    now = _utcnow()
    rows = conn.execute(
        """
        SELECT o.* FROM occupancies o
          LEFT JOIN drop_pickup_bonds b ON b.occupancy_id = o.id
         WHERE o.status='released' AND b.id IS NULL
         ORDER BY o.opened_at
        """
    ).fetchall()

    report = {"backfilled": 0, "quarantined": 0, "bond_ids": []}
    for occ in rows:
        pickup = _find_pickup_for(conn, occ)
        if pickup is None:
            _quarantine(
                conn,
                REASON_BACKFILL,
                {
                    "code": "backfill_missing_pickup",
                    "occupancy_id": occ["id"],
                    "cell_id": occ["cell_id"],
                    "tracking_no": occ["tracking_no"],
                },
                now,
            )
            report["quarantined"] += 1
            continue
        try:
            with atomic(conn, "backfill"):
                drop = resolve_drop_leg(conn, occ, pickup)
                bond_id = insert_bond(
                    conn, occ["id"], drop, pickup, now, BACKFILL_SOURCE
                )
        except (BondError, sqlite3.IntegrityError) as exc:
            _quarantine(
                conn,
                REASON_BACKFILL,
                {
                    "code": "backfill_missing_drop",
                    "occupancy_id": occ["id"],
                    "cell_id": occ["cell_id"],
                    "tracking_no": occ["tracking_no"],
                    "detail": str(exc),
                },
                now,
            )
            report["quarantined"] += 1
            continue
        report["backfilled"] += 1
        report["bond_ids"].append(bond_id)
    return report


# ---------------------------------------------------------------- 只读核证


def bond_row(conn, occupancy_id):
    return conn.execute(
        "SELECT * FROM drop_pickup_bonds WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()


def bond_for_occupancy(conn, occupancy_id: str) -> dict | None:
    """按占用查绑定边与两端事件；占用不存在返回 None。"""
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if occ is None:
        return None
    bond = bond_row(conn, occupancy_id)
    drop = pickup = None
    if bond is not None:
        drop = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (bond["drop_event_id"],)
        ).fetchone()
        pickup = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (bond["pickup_event_id"],)
        ).fetchone()
    return {
        "occupancy": dict(occ),
        "bond": dict(bond) if bond is not None else None,
        "drop_event": _event_view(drop),
        "pickup_event": _event_view(pickup),
    }


def list_bonds(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT b.*, o.cell_id AS occ_cell_id, o.status AS occ_status,
               o.tracking_no AS occ_tracking_no, o.opened_at, o.closed_at
          FROM drop_pickup_bonds b
          JOIN occupancies o ON o.id = b.occupancy_id
         ORDER BY o.opened_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        drop = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (row["drop_event_id"],)
        ).fetchone()
        pickup = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (row["pickup_event_id"],)
        ).fetchone()
        out.append(
            {
                "bond": {
                    "id": row["id"],
                    "occupancy_id": row["occupancy_id"],
                    "drop_event_id": row["drop_event_id"],
                    "pickup_event_id": row["pickup_event_id"],
                    "tracking_no": row["tracking_no"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                },
                "occupancy": {
                    "id": row["occupancy_id"],
                    "cell_id": row["occ_cell_id"],
                    "tracking_no": row["occ_tracking_no"],
                    "status": row["occ_status"],
                    "opened_at": row["opened_at"],
                    "closed_at": row["closed_at"],
                },
                "drop_event": _event_view(drop),
                "pickup_event": _event_view(pickup),
            }
        )
    return out


def bond_proofs(conn, limit: int = 200) -> list[dict]:
    """核证页数据源：每个占用一行，含两端事件与异常标记。"""
    rows = conn.execute(
        """
        SELECT o.id AS occupancy_id, o.cell_id, o.tracking_no, o.opened_at,
               o.closed_at, o.status,
               b.id AS bond_id, b.source AS bond_source,
               b.drop_event_id, b.pickup_event_id
          FROM occupancies o
          LEFT JOIN drop_pickup_bonds b ON b.occupancy_id = o.id
         ORDER BY o.opened_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()

    out = []
    for row in rows:
        drop = pickup = None
        if row["bond_id"] is not None:
            drop = conn.execute(
                "SELECT * FROM parcel_events WHERE id=?", (row["drop_event_id"],)
            ).fetchone()
            pickup = conn.execute(
                "SELECT * FROM parcel_events WHERE id=?", (row["pickup_event_id"],)
            ).fetchone()
        bonded = row["bond_id"] is not None
        anomaly = None
        if row["status"] == "released" and not bonded:
            anomaly = "缺绑定边：已释放但无 drop↔pickup 绑定边"
        elif row["status"] == "occupied" and bonded:
            anomaly = "绑定边存在但占用未释放"
        out.append(
            {
                "occupancy_id": row["occupancy_id"],
                "cell_id": row["cell_id"],
                "tracking_no": row["tracking_no"],
                "status": row["status"],
                "opened_at": row["opened_at"],
                "closed_at": row["closed_at"],
                "bond_id": row["bond_id"],
                "bond_source": row["bond_source"],
                "bonded": bonded,
                "drop_event": _event_view(drop),
                "pickup_event": _event_view(pickup),
                "anomaly": anomaly,
            }
        )
    return out
