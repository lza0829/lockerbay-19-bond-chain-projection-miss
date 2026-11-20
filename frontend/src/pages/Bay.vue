<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const overview = ref(null)
const bay = ref([])
const selected = ref(null)
const chain = ref(null)
const chainErr = ref('')

async function drill(cell) {
  if (!cell.occupancy) return
  selected.value = cell.occupancy.id
  chain.value = null
  chainErr.value = ''
  try { chain.value = await api.occupancyChain(cell.occupancy.id) }
  catch (e) { chainErr.value = String(e.message || e) }
}

onMounted(async () => {
  overview.value = await api.overview()
  bay.value = await api.bay()
})
</script>
<template>
  <div data-plant="19-bond-chain-projection-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>格阵</h1>
    <p class="sub">柜机格口占用态一眼可核；点格口下钻占用事件链，drop→pickup 双腿与时序对得上才算齐。</p>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.cells }}</b><span>格口</span></div>
      <div class="stat"><b>{{ overview.occupied }}</b><span>占用</span></div>
      <div class="stat"><b>{{ overview.free }}</b><span>空闲</span></div>
      <div class="stat"><b>{{ overview.open_occupancies }}</b><span>开放占用账</span></div>
    </div>
    <section v-for="cab in bay" :key="cab.id" class="cabinet panel">
      <h2>{{ cab.label }} <small>{{ cab.id }} · {{ cab.site }}</small></h2>
      <div class="grid">
        <div
          v-for="cell in cab.cells"
          :key="cell.id"
          class="cell"
          :class="[cell.status, { clickable: !!cell.occupancy, active: selected === cell.occupancy?.id }]"
          @click="drill(cell)"
        >
          <div>
            <div class="code">{{ cell.code }}</div>
            <div class="meta">{{ cell.size_code }} · {{ cell.status === 'occupied' ? '占用' : '空闲' }}</div>
          </div>
          <div class="meta" v-if="cell.status === 'occupied' && cell.occupancy?.tracking_no">{{ cell.occupancy.tracking_no }}</div>
          <span class="dot" />
        </div>
      </div>
    </section>

    <div v-if="selected" class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">
        占用事件链 <code>{{ selected }}</code>
        <span v-if="chain" class="badge" :class="{ ok: chain.complete }">
          {{ chain.complete ? '双腿齐 · 有序' : '待取件关账' }}
        </span>
      </h2>
      <p v-if="chainErr" class="badge">{{ chainErr }}</p>
      <template v-if="chain">
        <p class="sub" v-if="chain.bond_id">
          绑定边 <code>{{ chain.bond_id }}</code> · 投影来源 {{ chain.source }} ·
          两端时序 {{ chain.drop_event_ts }} → {{ chain.pickup_event_ts || '—' }}
        </p>
        <table>
          <thead><tr><th>#</th><th>事件</th><th>类型</th><th>运单</th><th>时刻</th></tr></thead>
          <tbody>
            <tr v-for="(e, i) in chain.events" :key="e.id">
              <td class="sub">{{ i + 1 }}</td>
              <td><code>{{ e.id }}</code></td>
              <td>{{ e.event_type }}</td>
              <td>{{ e.tracking_no || '—' }}</td>
              <td>{{ e.event_ts }}</td>
            </tr>
          </tbody>
        </table>
      </template>
      <p v-else-if="!chainErr" class="sub">链投影加载中…</p>
    </div>
  </div>
</template>

<style scoped>
.cell.clickable { cursor: pointer; }
.cell.active { outline: 2px solid var(--accent, #7ea6ff); }
.badge {
  display: inline-block;
  padding: .15rem .45rem;
  border-radius: 3px;
  border: 1px solid var(--over);
  color: var(--over);
  font-size: .8rem;
  margin-left: .5rem;
}
.badge.ok { border-color: var(--ok, #5fc98e); color: var(--ok, #5fc98e); }
</style>
