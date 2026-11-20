# 格湾（LockerBay）

**骨架：** 入柜/取件事件装载 → 格口占用物化 → 超时回收 → 占用账对账

- 领域：数据工程（去皮=事件装载 + 占用状态物化 + 超时闸/回收 + 占用核证）
- 端口：前端 **6100**，接口 **11100**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/lockerbay-NN-*`
- 非仓库库存 CRUD、非运单轨迹轴、非营业日封账、非会话窗切分

**本功能：投取事件绑定核证（drop ↔ pickup bond proof）+ 占用事件链投影**

- 关占用（取件释放）必须物化一条 `drop_pickup_bonds` 绑定边（占用 id / drop 事件 id / pickup 事件 id / 运单）。
- 独立 bond 边表存放；同事务写边 + 关占用 + 改格口，失败整体回滚。
- 关账绑定标成功必须**同成败**落事件链投影：独立 `occupancy_event_chain` 表（占用 id / 绑定边 id / 有序 event_id 列表 / 两端点时序），写边同事务重算并落链，缺一回滚。
- 链 = 占用窗口 `[drop_ts, pickup_ts]` 内同格口同运单事件按时序重放，首 drop 末 pickup；绑定成功而链缺 pickup、链序与事件时序分叉，都拒关账并搁置。
- 库层触发器：无绑定边不得把占用改 `released`；有边无链同样不得 `released`；绑定边两端事件必须存在、类型正确、运单一致；链端点必须等于绑定边两端且时序与事件库一致；链投影只增不改不删。
- 缺 drop 侧：拒关账，占用与格口保持占用态，写 `quarantine` 搁置。
- 存量已释放无边的占用：启动时补边（`source='backfill'`）；有边无链的启动补链投影；补不齐写搁置。
- 装载关账回执双载：`close_receipts` 同时携带绑定边与链投影（有序链、两端时序），不只写 bond_ok。
- 只读核证：`GET /api/bond-proofs`、`GET /api/occupancy-bonds[/{occupancy_id}]`；事件链核证 `GET /api/chain-proofs`、`GET /api/occupancy-chain/{occupancy_id}/verify`（重放事件库与投影、绑定边三方互证）；格阵下钻 `GET /api/occupancy-chain/{occupancy_id}` 返回完整有序链。核账页边链同列，格阵点格口下钻事件链。

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
