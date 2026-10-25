<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError, api } from '../api'

const route = useRoute()

const STATUS = {
  1: ['未付款', 'warn'],
  2: ['已付款待发货', 'ok'],
  3: ['已发货', 'ok'],
  4: ['已收货', 'ok'],
  5: ['交易完成', 'ok'],
  6: ['已退款', 'muted'],
  7: ['已关闭', 'muted'],
}

const FILTERS = [
  ['全部', null],
  ['未付款', 1],
  ['待发货', 2],
  ['已发货', 3],
  ['已完成', 5],
  ['已关闭', 7],
]

const orders = ref([])
const loading = ref(true)
const error = ref('')
const notice = ref('')
const filter = ref(null)
const openId = ref(null)
const detail = ref(null)
const busyId = ref(null)

const createdNo = computed(() => String(route.query.created || ''))

function label(s) {
  return STATUS[s]?.[0] || `状态 ${s}`
}
function tone(s) {
  return STATUS[s]?.[1] || ''
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    orders.value = await api.orders(filter.value ?? undefined)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '加载失败'
    orders.value = []
  } finally {
    loading.value = false
  }
}

async function pick(f) {
  filter.value = f
  openId.value = null
  detail.value = null
  load()
}

async function toggle(o) {
  if (openId.value === o.id) {
    openId.value = null
    return
  }
  openId.value = o.id
  detail.value = null
  try {
    detail.value = await api.orderDetail(o.id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '加载详情失败'
    openId.value = null
  }
}

async function act(o, fn, msg) {
  error.value = ''
  notice.value = ''
  busyId.value = o.id
  try {
    const updated = await fn(o.id)
    // 后端返回的是改动后的订单行，就地替换，省一次整表重拉
    orders.value = orders.value.map((x) => (x.id === updated.id ? updated : x))
    if (detail.value?.id === updated.id) detail.value = { ...detail.value, ...updated }
    notice.value = msg
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '操作失败'
  } finally {
    busyId.value = null
  }
}

const pay = (o) => act(o, api.payOrder, '支付成功（模拟），状态已变为待发货')
const cancel = (o) => act(o, api.cancelOrder, '订单已取消，库存已归还')

function stamp(t) {
  return t ? t.replace('T', ' ').slice(0, 19) : ''
}

load()
</script>

<template>
  <h2 style="margin: 0 0 14px; font-size: 19px">我的订单</h2>

  <div class="tabs">
    <button
      v-for="[name, f] in FILTERS"
      :key="String(f)"
      :class="{ on: filter === f }"
      @click="pick(f)"
    >
      {{ name }}
    </button>
  </div>

  <p v-if="error" class="card err" style="margin-bottom: 12px">{{ error }}</p>
  <p v-if="notice" class="card ok" style="margin-bottom: 12px">{{ notice }}</p>

  <p v-if="loading" class="card muted pad">加载中…</p>
  <p v-else-if="!orders.length" class="card muted pad">
    暂无订单。去<a href="javascript:void 0" class="link" @click="$router.push('/search')">搜一件商品</a>，
    加入购物车后在购物车页提交订单。
  </p>

  <section v-else class="card list">
    <div v-for="o in orders" :key="o.id" class="item" :class="{ new: o.order_no === createdNo }">
      <div class="head">
        <button class="disc" @click="toggle(o)">{{ openId === o.id ? '▾' : '▸' }}</button>
        <div class="id">
          <div>
            单号 <b>{{ o.order_no }}</b>
            <span v-if="o.order_no === createdNo" class="tag semantic">刚创建</span>
          </div>
          <div class="muted small">{{ stamp(o.created_at) }} 下单</div>
        </div>
        <div class="st" :class="tone(o.status)">{{ label(o.status) }}</div>
        <div class="amt">¥{{ Number(o.total_price).toFixed(2) }}</div>
        <div class="op">
          <template v-if="o.status === 1">
            <button class="primary sm" :disabled="busyId === o.id" @click="pay(o)">
              {{ busyId === o.id ? '处理中…' : '模拟支付' }}
            </button>
            <button class="ghost sm" :disabled="busyId === o.id" @click="cancel(o)">取消</button>
          </template>
          <span v-else class="muted small">—</span>
        </div>
      </div>

      <div class="recv muted small">
        {{ o.receiver_name }} {{ o.receiver_phone }} ｜ {{ o.receiver_address }}
        <span v-if="o.status === 7" class="muted">（{{ o.close_reason }}）</span>
      </div>

      <div v-if="openId === o.id" class="body">
        <p v-if="!detail" class="muted">详情加载中…</p>
        <template v-else>
          <div v-for="it in detail.items" :key="it.id" class="line">
            <img v-if="it.goods_image" :src="it.goods_image" :alt="it.goods_name" class="thumb" />
            <div v-else class="thumb">{{ it.goods_name.slice(0, 2) }}</div>
            <RouterLink :to="`/goods/${it.goods_id}`" class="gname">{{ it.goods_name }}</RouterLink>
            <span class="muted small">
              {{ Object.entries(it.spec_values || {}).map(([k, v]) => `${k}: ${v}`).join('　') }}
            </span>
            <span class="muted small sp">¥{{ Number(it.goods_price).toFixed(2) }} × {{ it.quantity }}</span>
          </div>
          <dl class="times">
            <template v-for="t in [
              ['下单', detail.created_at],
              ['付款', detail.pay_time],
              ['发货', detail.consign_time],
              ['收货', detail.confirm_time],
              ['完成', detail.finish_time],
              ['关闭', detail.close_time],
            ]" :key="t[0]">
              <dt>{{ t[0] }}</dt>
              <dd>{{ t[1] ? stamp(t[1]) : '—' }}</dd>
            </template>
          </dl>
        </template>
      </div>
    </div>
  </section>

  <p class="muted tiny">
    本项目没有后台发货流程，所以界面上只能走到「已付款待发货」；3、4、5、6 这几个状态需要人工或对接物流后写入，
    但订单状态机、超时关单和 AI 客服查单对它们的解读都是真实可用的 —— 可以直接问客服「我那单发货了吗」。
  </p>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 6px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.tabs button {
  padding: 6px 14px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: #fff;
  color: var(--muted);
  font-size: 13px;
}
.tabs button.on {
  background: #eef2ff;
  border-color: #c7d2fe;
  color: var(--brand);
  font-weight: 600;
}
.pad {
  padding: 24px;
}
.list {
  padding: 0 18px;
}
.item {
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
}
.item:last-child {
  border-bottom: none;
}
.item.new {
  background: #f8faff;
  border-radius: 8px;
  padding-left: 10px;
  padding-right: 10px;
  outline: 1px solid #c7d2fe;
}
.head {
  display: grid;
  grid-template-columns: 22px 1fr 108px 96px 168px;
  gap: 10px;
  align-items: center;
}
.disc {
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  background: none;
  color: var(--muted);
  line-height: 1;
}
.id {
  min-width: 0;
  font-size: 13px;
}
.st {
  font-size: 13px;
  font-weight: 600;
}
.st.warn {
  color: var(--warn);
}
.st.ok {
  color: var(--ok);
}
.st.muted {
  color: var(--muted);
  font-weight: 400;
}
.amt {
  text-align: right;
  font-weight: 700;
  color: #b91c1c;
}
.op {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  align-items: center;
}
.sm {
  padding: 5px 12px;
  font-size: 13px;
}
.sm:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.recv {
  margin: 8px 0 0 32px;
}
.small {
  font-size: 12px;
}
.body {
  margin: 12px 0 4px 32px;
  padding: 12px;
  background: #f8fafc;
  border-radius: 8px;
}
.line {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
}
.thumb {
  width: 36px;
  height: 36px;
  border-radius: 6px;
  background: linear-gradient(135deg, #dbeafe, #e0e7ff);
  color: #3730a3;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
  object-fit: cover;
}
.gname {
  color: inherit;
  text-decoration: none;
  font-size: 13px;
}
.gname:hover {
  color: var(--brand);
}
.sp {
  margin-left: auto;
  white-space: nowrap;
}
.times {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 4px 14px;
  margin: 12px 0 0;
  padding-top: 10px;
  border-top: 1px dashed var(--line);
  font-size: 12px;
}
.times dt {
  color: var(--muted);
}
.times dd {
  margin: 0;
  color: #374151;
}
.tiny {
  font-size: 12px;
  margin: 12px 2px 0;
}
.link {
  color: var(--brand);
}
.ok {
  color: var(--ok);
  font-size: 13px;
}
</style>
