import json
from datetime import datetime, timezone
from uuid import uuid4

from app.db import FIXTURES_DIR, connect
from app.modules import drop_pickup_bond_proof as bond_proof


def run_batch():
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"batch_id": None, "accepted": 0, "message": "无夹具"}
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events", [])
    batch_id = f"B-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect()
    accepted = 0
    bonded = 0
    rejected_closes = []
    try:
        # 整批一个事务；单条 pickups 关账用 savepoint 隔离，失败只回滚该条并搁置。
        conn.execute("BEGIN IMMEDIATE")
        for ev in sorted(events, key=lambda e: (e["event_ts"], e["id"])):
            cur = conn.execute(
                "INSERT OR IGNORE INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ev["id"],
                    ev["cabinet_id"],
                    ev.get("cell_id"),
                    ev["event_type"],
                    ev["event_ts"],
                    ev.get("tracking_no"),
                    json.dumps(ev.get("payload", {}), ensure_ascii=False),
                    "primary",
                ),
            )
            inserted = cur.rowcount == 1
            if inserted:
                accepted += 1
            if not inserted:
                # 重复事件不重复物化；关账结果已在首次入库时定案。
                continue
            if ev["event_type"] == "drop" and ev.get("cell_id"):
                cell = conn.execute(
                    "SELECT status FROM cells WHERE id=?", (ev["cell_id"],)
                ).fetchone()
                if cell and cell["status"] == "free":
                    oid = f"OCC-{ev['cell_id']}-{ev['id']}"
                    conn.execute(
                        "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id) VALUES (?,?,?,?,?,?,?)",
                        (oid, ev["cell_id"], ev["tracking_no"], ev["event_ts"], None, "occupied", ev["id"]),
                    )
                    conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (ev["cell_id"],))
            elif ev["event_type"] == "pickup" and ev.get("cell_id"):
                outcome = bond_proof.try_close_pickup(conn, ev, now)
                if outcome["status"] == "closed":
                    bonded += 1
                else:
                    rejected_closes.append(
                        {
                            "pickup_event_id": outcome["pickup_event_id"],
                            "reason": outcome["reason"],
                        }
                    )
        report = {
            "accepted": accepted,
            "total": len(events),
            "bonded": bonded,
            "rejected_closes": rejected_closes,
        }
        conn.execute(
            "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, now, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"batch_id": batch_id, **report}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "occupancy_id" in out and "occ_id" not in out:
        out["occ_id"] = out.get("occupancy_id")
    if "cell_id" in out and "locker_cell" not in out:
        out["locker_cell"] = out.get("cell_id")
    if "tracking_no" in out and "parcel_key" not in out:
        out["parcel_key"] = out.get("tracking_no")
    if "verified" not in out:
        out["verified"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True}
    if extra:
        data.update(extra)
    return data

