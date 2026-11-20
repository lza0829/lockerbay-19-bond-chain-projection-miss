<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const chains = ref({})
const selected = ref(null)
const detail = ref(null)
const verdict = ref(null)
const err = ref('')
const onlyAnomaly = ref(false)

async function load() {
  const [bp, cp] = await Promise.all([api.bondProofs(), api.chainProofs()])
  proofs.value = bp
  chains.value = Object.fromEntries(cp.map((c) => [c.occupancy_id, c]))
}
function chainOf(occupancyId) { return chains.value[occupancyId] || null }
function anomalyOf(p) {
  const c = chainOf(p.occupancy_id)
  return p.anomaly || (c && !c.ok ? c.anomaly || '事件链核证未过' : null)
}
async function inspect(row) {
  err.value = ''
  detail.value = null
  verdict.value = null
  selected.value = row.occupancy_id
  try {
    ;[detail.value, verdict.value] = await Promise.all([
      api.bondFor(row.occupancy_id),
      api.verifyChain(row.occupancy_id),
    ])
  } catch (e) { err.value = String(e.message || e) }
}
function visible() {
  return onlyAnomaly.value ? proofs.value.filter((p) => anomalyOf(p)) : proofs.value
}
onMounted(load)
</script>
<template>
  <div data-plant="19-bond-chain-projection-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">关占用必须物化 drop↔pickup 绑定边，且同事务落事件链投影；边与链互证，缺腿、断链、时序分叉即标红。</p>
    <p class="toolbar">
      <button @click="load">刷新</button>
      <label class="sub"><input v-model="onlyAnomaly" type="checkbox" /> 只看异常</label>
      <span class="sub">
        共 {{ proofs.length }} 条占用 · 已绑定 {{ proofs.filter((p) => p.bonded).length }} ·
        异常 {{ proofs.filter((p) => anomalyOf(p)).length }}
      </span>
    </p>
    <div class="panel" style="overflow:auto">
      <table>
        <thead>
          <tr>
            <th>占用</th><th>格口</th><th>运单</th><th>状态</th>
            <th>drop 事件</th><th>pickup 事件</th><th>绑定边</th><th>事件链</th><th>核对</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in visible()" :key="p.occupancy_id" :class="{ bad: anomalyOf(p) }" @click="inspect(p)">
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
              <template v-if="chainOf(p.occupancy_id)?.chain">
                <code>{{ chainOf(p.occupancy_id).chain.event_ids.join(' → ') }}</code>
                <div class="sub">
                  {{ chainOf(p.occupancy_id).chain.event_count }} 事件 ·
                  {{ chainOf(p.occupancy_id).chain.drop_event_ts }} →
                  {{ chainOf(p.occupancy_id).chain.pickup_event_ts }}
                </div>
              </template>
              <span v-else-if="chainOf(p.occupancy_id)?.state === 'open'" class="sub">待取件关账</span>
              <span v-else class="sub">—</span>
            </td>
            <td>
              <span v-if="anomalyOf(p)" class="badge">{{ anomalyOf(p) }}</span>
              <span v-else class="sub">边链互证齐</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!visible().length" class="sub">暂无占用核对。</p>
    </div>

    <div v-if="selected" class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用 {{ selected }}</h2>
      <p v-if="err" class="badge">{{ err }}</p>
      <template v-if="verdict">
        <p>
          <span class="badge" :class="{ ok: verdict.ok }">
            {{ verdict.state === 'open' ? '开放占用 · 待关账' : verdict.ok ? '链核证通过' : '链核证异常' }}
          </span>
          <span v-for="a in verdict.anomalies" :key="a" class="badge">{{ a }}</span>
        </p>
        <table v-if="verdict.checks && Object.keys(verdict.checks).length" style="margin-bottom:.75rem">
          <thead><tr><th>核证项</th><th>结果</th></tr></thead>
          <tbody>
            <tr v-for="(v, k) in verdict.checks" :key="k">
              <td><code>{{ k }}</code></td>
              <td>{{ v ? '✓' : '✗' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
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
  margin-right: .4rem;
}
.badge.ok { border-color: var(--ok, #5fc98e); color: var(--ok, #5fc98e); }
tr { cursor: pointer; }
pre { margin: 0; overflow: auto; font-size: .82rem; }
</style>
