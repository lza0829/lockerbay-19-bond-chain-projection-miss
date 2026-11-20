<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const rows = ref([])
const q = ref([])
async function load() {
  const [occ, bp, quarantine] = await Promise.all([
    api.occupancies(),
    api.bondProofs(),
    api.quarantine(),
  ])
  const byOcc = Object.fromEntries(bp.map((p) => [p.occupancy_id, p]))
  rows.value = occ.map((r) => ({ ...r, bond: byOcc[r.id] || null }))
  q.value = quarantine
}
onMounted(load)
</script>
<template>
  <div data-plant="19-bond-chain-projection-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>回收</h1>
    <p class="sub">占用账逐条带出绑定边；已释放却无边的行标红，属账不平。</p>
    <div class="panel">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用账</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>状态</th><th>开启</th><th>绑定边</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id" :class="{ bad: r.bond?.anomaly }">
            <td><code>{{ r.id }}</code></td>
            <td>{{ r.cell_id }}</td>
            <td>{{ r.tracking_no }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.opened_at }}</td>
            <td>
              <template v-if="r.bond?.bond_id">
                <code>{{ r.bond.bond_id }}</code>
                <div class="sub">{{ r.bond.drop_event?.id }} → {{ r.bond.pickup_event?.id }}</div>
              </template>
              <span v-else-if="r.bond?.anomaly" class="badge">{{ r.bond.anomaly }}</span>
              <span v-else class="sub">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">搁置</h2>
      <p v-if="!q.length" class="sub">暂无搁置。</p>
      <table v-else>
        <thead><tr><th>id</th><th>原因</th><th>成员</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="item in q" :key="item.id">
            <td>{{ item.id }}</td>
            <td>{{ item.reason }}</td>
            <td><code>{{ item.member_json }}</code></td>
            <td>{{ item.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
tr.bad { background: rgba(196, 92, 106, .12); }
.badge {
  display: inline-block;
  padding: .15rem .45rem;
  border-radius: 3px;
  border: 1px solid var(--over);
  color: var(--over);
  font-size: .8rem;
}
</style>
