<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const selected = ref(null)
const detail = ref(null)
const err = ref('')
const onlyAnomaly = ref(false)

async function load() { proofs.value = await api.bondProofs() }
async function inspect(row) {
  err.value = ''
  detail.value = null
  selected.value = row.occupancy_id
  try { detail.value = await api.bondFor(row.occupancy_id) }
  catch (e) { err.value = String(e.message || e) }
}
function visible() {
  return onlyAnomaly.value ? proofs.value.filter((p) => p.anomaly) : proofs.value
}
onMounted(load)
</script>
<template>
  <div data-plant="19-bond-chain-projection-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">关占用必须物化 drop↔pickup 绑定边；下表按占用列出两端事件，缺边即标红。</p>
    <p class="toolbar">
      <button @click="load">刷新</button>
      <label class="sub"><input v-model="onlyAnomaly" type="checkbox" /> 只看异常</label>
      <span class="sub">
        共 {{ proofs.length }} 条占用 · 已绑定 {{ proofs.filter((p) => p.bonded).length }} ·
        异常 {{ proofs.filter((p) => p.anomaly).length }}
      </span>
    </p>
    <div class="panel" style="overflow:auto">
      <table>
        <thead>
          <tr>
            <th>占用</th><th>格口</th><th>运单</th><th>状态</th>
            <th>drop 事件</th><th>pickup 事件</th><th>绑定边</th><th>核对</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in visible()" :key="p.occupancy_id" :class="{ bad: p.anomaly }" @click="inspect(p)">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.status }}</td>
            <td>
              <template v-if="p.drop_event">
                <code>{{ p.drop_event.id }}</code>
                <div class="sub">{{ p.drop_event.event_ts }} · {{ p.drop_event.tracking_no }}</div>
              </template>
              <span v-else class="sub">—</span>
            </td>
            <td>
              <template v-if="p.pickup_event">
                <code>{{ p.pickup_event.id }}</code>
                <div class="sub">{{ p.pickup_event.event_ts }} · {{ p.pickup_event.tracking_no }}</div>
              </template>
              <span v-else class="sub">—</span>
            </td>
            <td>
              <template v-if="p.bond_id">
                <code>{{ p.bond_id }}</code>
                <div class="sub">{{ p.bond_source === 'backfill' ? '存量补边' : '关账写边' }}</div>
              </template>
              <span v-else class="sub">—</span>
            </td>
            <td>
              <span v-if="p.anomaly" class="badge">{{ p.anomaly }}</span>
              <span v-else class="sub">两端齐</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!visible().length" class="sub">暂无占用核对。</p>
    </div>

    <div v-if="selected" class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用 {{ selected }}</h2>
      <p v-if="err" class="badge">{{ err }}</p>
      <pre v-if="detail">{{ JSON.stringify(detail, null, 2) }}</pre>
    </div>
  </div>
</template>

<style scoped>
tr.bad { background: rgba(196, 92, 106, .12); }
.toolbar { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
.badge {
  display: inline-block;
  padding: .15rem .45rem;
  border-radius: 3px;
  border: 1px solid var(--over);
  color: var(--over);
  font-size: .8rem;
}
tr { cursor: pointer; }
pre { margin: 0; overflow: auto; font-size: .82rem; }
</style>
