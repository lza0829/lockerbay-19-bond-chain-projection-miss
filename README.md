# 格湾（LockerBay）

**骨架：** 入柜/取件事件装载 → 格口占用物化 → 超时回收 → 占用账对账

- 领域：数据工程（去皮=事件装载 + 占用状态物化 + 超时闸/回收 + 占用核证）
- 端口：前端 **6100**，接口 **11100**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/lockerbay-NN-*`
- 非仓库库存 CRUD、非运单轨迹轴、非营业日封账、非会话窗切分

**本功能：投取事件绑定核证（drop ↔ pickup bond proof）**

- 关占用（取件释放）必须物化一条 `drop_pickup_bonds` 绑定边（占用 id / drop 事件 id / pickup 事件 id / 运单）。
- 独立 bond 边表存放；同事务写边 + 关占用 + 改格口，失败整体回滚。
- 库层触发器：无绑定边不得把占用改 `released`；绑定边两端事件必须存在、类型正确、运单一致。
- 缺 drop 侧：拒关账，占用与格口保持占用态，写 `quarantine` 搁置。
- 存量已释放无边的占用：启动时补边（`source='backfill'`），补不齐写搁置。
- 只读核证：`GET /api/bond-proofs`、`GET /api/occupancy-bonds[/{occupancy_id}]`；核账页展示两端事件。

本地冒烟：

```powershell
cd claude-workspace/lockerbay/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11100

cd ../frontend
npm install
npm run dev
```

测试（SQLite 本地夹具，无需外部服务）：

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt httpx pytest
.venv/bin/python -m pytest -q
```
