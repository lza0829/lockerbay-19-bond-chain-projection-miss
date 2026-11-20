<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const events = ref([])
const report = ref(null)
async function load() { events.value = await api.events() }
async function run() { report.value = await api.ingest(); await load() }
onMounted(load)
</script>
<template>
  <div data-plant="19-bond-chain-projection-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>装载</h1>
    <p class="sub">投柜 / 取件事件进入主通道；基座按顺序物化占用。</p>
    <p><button @click="run">跑一批夹具</button></p>
    <pre v-if="report" class="panel">{{ report }}</pre>
    <div v-if="report?.rejected_closes?.length" class="panel" style="margin-top:1rem; border-color:var(--over)">
      <h2 style="margin:0 0 .5rem; font-size:1rem">拒关账（缺 drop 侧已搁置）</h2>
      <table>
        <thead><tr><th>pickup 事件</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="r in report.rejected_closes" :key="r.pickup_event_id">
            <td><code>{{ r.pickup_event_id }}</code></td>
            <td>{{ r.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel" style="margin-top:1rem; overflow:auto">
      <table>
        <thead><tr><th>事件</th><th>类型</th><th>格口</th><th>运单</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="e in events" :key="e.id">
            <td><code>{{ e.id }}</code></td>
            <td>{{ e.event_type }}</td>
            <td>{{ e.cell_id || '—' }}</td>
            <td>{{ e.tracking_no || '—' }}</td>
            <td>{{ e.event_ts }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
