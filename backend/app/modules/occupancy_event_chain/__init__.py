
STATUS = "partial"


def chain_for(conn, occupancy_id: str) -> dict:
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if not occ:
        return {"occupancy_id": occupancy_id, "events": [], "ordered": False}
    # 只挂 last_event，不重放完整 drop→pickup 链
    ev = occ["last_event_id"]
    return {
        "occupancy_id": occupancy_id,
        "events": [ev] if ev else [],
        "ordered": False,
        "complete": False,
    }
