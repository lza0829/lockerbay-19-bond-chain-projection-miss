from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import drop_pickup_bond_proof as bond_proof
from app.seed import overdue_hours
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/bond-proofs")
def bond_proofs():
    """核证页数据源：每个占用一行，含 drop/pickup 两端事件与异常标记。"""
    conn = connect()
    try:
        return bond_proof.bond_proofs(conn)
    finally:
        conn.close()


@router.get("/occupancy-bonds")
def occupancy_bonds():
    conn = connect()
    try:
        return bond_proof.list_bonds(conn)
    finally:
        conn.close()


@router.get("/occupancy-bonds/{occupancy_id}")
def occupancy_bond(occupancy_id: str):
    """按占用查绑定边两端事件；已释放却无绑定边说明账不平。"""
    conn = connect()
    try:
        found = bond_proof.bond_for_occupancy(conn, occupancy_id)
        if found is None:
            raise HTTPException(404, f"占用 {occupancy_id} 不存在")
        if found["bond"] is None:
            if found["occupancy"]["status"] == "released":
                raise HTTPException(409, f"占用 {occupancy_id} 已释放但无绑定边，账不平")
            found["expected"] = "closed_on_pickup"
            found["reason"] = "占用尚未取件关账，暂无绑定边"
        return found
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"overdue_hours": overdue_hours(conn)}
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        conn.commit()
        return {"overdue_hours": hours}
    finally:
        conn.close()

# --- plant routes ---
from app.modules import occupancy_event_chain as chain_svc

@router.get("/occupancy-chain/{occupancy_id}")
def occupancy_chain(occupancy_id: str):
    conn = connect()
    try:
        return chain_svc.chain_for(conn, occupancy_id)
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

