<script setup>
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError, api } from '../api'

const router = useRouter()

const items = ref([])
const addrs = ref([])
const addrId = ref(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const placing = ref(false)
const showAddr = ref(false)

const form = reactive({
  contact: '',
  mobile: '',
  province_name: '',
  city_name: '',
  area_name: '',
  detail: '',
  is_default: true,
})

// current_price 为 null 表示商品已删/已下架，这时只能用快照价兜底显示，
// 但这条永远不允许结算（available 一定是 false）
function unitPrice(it) {
  return Number(it.current_price ?? it.goods_price)
}
function lineTotal(it) {
  return unitPrice(it) * it.quantity
}

// 后端返回的整张购物车会覆盖本地列表，勾选状态按 id 继承；
// 新出现的条目按"能买就默认勾上"处理
function applyCart(rows) {
  const prev = new Map(items.value.map((i) => [i.id, i.checked]))
  items.value = rows.map((r) => ({
    ...r,
    checked: prev.has(r.id) ? prev.get(r.id) : r.available,
  }))
}

const selectable = computed(() => items.value.filter((i) => i.available))
const picked = computed(() => items.value.filter((i) => i.checked && i.available))
const total = computed(() => picked.value.reduce((s, i) => s + lineTotal(i), 0))
const allChecked = computed(
  () => selectable.value.length > 0 && selectable.value.every((i) => i.checked),
)

function toggleAll() {
  const next = !allChecked.value
  for (const it of items.value) it.checked = it.available ? next : false
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 两个请求互不依赖，并发发出去；等第二个再发第一个等于白多一个 RTT
    const [cart, addresses] = await Promise.all([api.cart(), api.addresses()])
    applyCart(cart)
    addrs.value = addresses
    if (!addrId.value) {
      addrId.value = (addresses.find((a) => a.is_default) || addresses[0])?.id ?? null
    }
    if (!addresses.length) showAddr.value = true
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function setQty(it, qty) {
  if (qty < 1 || qty === it.quantity || qty > it.current_stock) return
  error.value = ''
  try {
    applyCart(await api.setCartQty(it.id, qty))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '修改数量失败'
  }
}

async function removeItems(ids) {
  error.value = ''
  notice.value = ''
  try {
    applyCart(await api.removeCart(ids))
    notice.value = `已移除 ${ids.length} 项`
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '移除失败'
  }
}

const removeOne = (it) => removeItems([it.id])

function removeSelected() {
  const ids = items.value.filter((i) => i.checked).map((i) => i.id)
  if (!ids.length) {
    notice.value = '先勾选要移除的商品'
    return
  }
  removeItems(ids)
}

async function saveAddress() {
  error.value = ''
  try {
    const created = await api.addAddress({ ...form })
    addrs.value = [...addrs.value, created]
    addrId.value = created.id
    showAddr.value = false
    notice.value = '地址已保存'
  } catch (e) {
    // 字段校验失败时后端返回的是 pydantic 的聚合消息，直接展示就够定位了
    error.value = e instanceof ApiError ? e.message : '地址保存失败'
  }
}

// Idempotency-Key 自己拼，不用 crypto.randomUUID()：
// 那个 API 只在安全上下文（https 或 localhost）里存在，用局域网 IP 打开页面就是 undefined，
// 表现是"换台机器点提交订单毫无反应"。这里要的只是"同一次点击唯一"，不是抗猜测。
function submitKey() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

async function checkout() {
  notice.value = ''
  error.value = ''
  if (!picked.value.length) {
    error.value = '请先勾选要购买的商品'
    return
  }
  if (!addrId.value) {
    error.value = '请先填写收货地址'
    showAddr.value = true
    return
  }
  placing.value = true
  try {
    // 每次点击生成一个新的 Idempotency-Key：它是"这一下提交"的身份，
    // 不是"这个购物车"的身份。连点保护靠的是 placing 标志位。
    const order = await api.createOrder(
      { cart_item_ids: picked.value.map((i) => i.id), address_id: addrId.value },
      submitKey(),
    )
    router.push({ path: '/orders', query: { created: order.order_no } })
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '下单失败'
    // 库存是被别的请求抢掉的，本地这份列表已经过期，必须重新拉
    if (e instanceof ApiError && e.code === 409) load()
  } finally {
    placing.value = false
  }
}

function addrText(a) {
  // full_address 是后端 AddressOut 的 computed_field，前端不再自己拼三段区划
  return a.full_address
}

load()
</script>

<template>
  <h2 style="margin: 0 0 14px; font-size: 19px">购物车</h2>

  <p v-if="loading" class="card muted pad">加载中…</p>
  <p v-else-if="error && !items.length" class="card err pad">{{ error }}</p>

  <p v-else-if="!items.length" class="card muted pad">
    购物车是空的。去<a href="javascript:void 0" class="link" @click="router.push('/search')">商品搜索</a>挑一件。
  </p>

  <template v-else>
    <section class="card">
      <div class="hd">
        <label class="chk">
          <input type="checkbox" :checked="allChecked" @change="toggleAll" />
          <span class="muted">全选</span>
        </label>
        <span class="muted col">商品</span>
        <span class="muted col ctr">单价</span>
        <span class="muted col ctr">数量</span>
        <span class="muted col rgt">小计</span>
        <span class="muted col rgt">操作</span>
      </div>

      <div v-for="it in items" :key="it.id" class="row" :class="{ off: !it.available }">
        <label class="chk">
          <input v-model="it.checked" type="checkbox" :disabled="!it.available" />
        </label>

        <RouterLink :to="`/goods/${it.goods_id}`" class="goods">
          <img v-if="it.goods_image" :src="it.goods_image" :alt="it.goods_name" class="thumb" />
          <div v-else class="thumb">{{ it.goods_name.slice(0, 2) }}</div>
          <div class="info">
            <div class="name">{{ it.goods_name }}</div>
            <div v-if="it.spec_values" class="cap muted">
              {{ Object.entries(it.spec_values).map(([k, v]) => `${k}: ${v}`).join('　') }}
            </div>
            <div v-if="it.price_changed" class="tag fuzzy">价格已从 ¥{{ it.goods_price }} 变为 ¥{{ it.current_price }}</div>
            <div v-if="!it.available" class="tag">
              {{ it.insufficient_stock ? `库存只剩 ${it.current_stock}` : '已下架或删除' }}
            </div>
            <div v-else-if="it.insufficient_stock" class="tag fuzzy">
              库存只剩 {{ it.current_stock }}，点「−」减少数量
            </div>
          </div>
        </RouterLink>

        <div class="col ctr">¥{{ unitPrice(it).toFixed(2) }}</div>

        <div class="col ctr qty">
          <button class="step" :disabled="it.quantity <= 1" @click="setQty(it, it.quantity - 1)">−</button>
          <span>{{ it.quantity }}</span>
          <button class="step" :disabled="it.quantity >= it.current_stock" @click="setQty(it, it.quantity + 1)">+</button>
        </div>

        <div class="col rgt money">¥{{ lineTotal(it).toFixed(2) }}</div>

        <div class="col rgt">
          <button class="ghost del" @click="removeOne(it)">移除</button>
        </div>
      </div>

      <p v-if="error" class="err" style="margin: 12px 0 0">{{ error }}</p>
      <p v-if="notice" class="ok" style="margin: 12px 0 0">{{ notice }}</p>
    </section>

    <section class="card" style="margin-top: 16px">
      <h3>收货地址</h3>

      <div v-if="addrs.length" class="addrlist">
        <label v-for="a in addrs" :key="a.id" class="addr" :class="{ on: addrId === a.id }">
          <input v-model="addrId" type="radio" :value="a.id" name="addr" />
          <span class="who">
            {{ a.contact }} {{ a.mobile }}
            <span v-if="a.is_default" class="tag">默认</span>
          </span>
          <span class="muted detail">{{ addrText(a) }}</span>
        </label>
      </div>
      <p v-else class="muted">还没有收货地址，下面填一个。没有地址无法下单。</p>

      <button v-if="addrs.length && !showAddr" class="ghost" style="margin-top: 10px" @click="showAddr = true">
        + 新增地址
      </button>

      <div v-if="showAddr" class="form">
        <div class="grid">
          <label>联系人<input v-model="form.contact" placeholder="张三" /></label>
          <label>手机号<input v-model="form.mobile" placeholder="13800138000" maxlength="11" /></label>
          <label>省<input v-model="form.province_name" placeholder="广东省" /></label>
          <label>市<input v-model="form.city_name" placeholder="深圳市" /></label>
          <label>区<input v-model="form.area_name" placeholder="南山区" /></label>
          <label>详细地址<input v-model="form.detail" placeholder="科技园路 1 号 A 栋 101" /></label>
        </div>
        <label class="chk" style="margin-top: 6px">
          <input v-model="form.is_default" type="checkbox" /><span class="muted">设为默认地址</span>
        </label>
        <div style="display: flex; gap: 8px; margin-top: 10px">
          <button class="primary" @click="saveAddress">保存地址</button>
          <button v-if="addrs.length" class="ghost" @click="showAddr = false">取消</button>
        </div>
        <p class="muted tiny">
          省市区是三个自由文本框：原项目的三级联动字典表在本次重构范围里被砍掉了，
          订单只要求收货信息是字符串快照。
        </p>
      </div>
    </section>

    <section class="card settle">
      <div>
        已选 <b>{{ picked.length }}</b> 件，合计
        <b class="total">¥{{ total.toFixed(2) }}</b>
      </div>
      <div class="acts">
        <button class="ghost" @click="removeSelected">移除所选</button>
        <button class="primary big" :disabled="placing || !picked.length" @click="checkout">
          {{ placing ? '提交中…' : '提交订单' }}
        </button>
      </div>
    </section>
    <p class="muted tiny">
      提交订单会按当前实时价扣减库存并生成未付款订单；30 分钟未支付由定时任务自动关单并归还库存。
    </p>
  </template>
</template>

<style scoped>
.pad {
  padding: 24px;
}
.hd,
.row {
  display: grid;
  grid-template-columns: 26px 1fr 90px 108px 92px 68px;
  gap: 10px;
  align-items: center;
}
.hd {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
  font-size: 12px;
}
.row {
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
}
.row:last-of-type {
  border-bottom: none;
}
.row.off {
  opacity: 0.6;
}
.chk {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
}
.col {
  font-size: 13px;
}
.ctr {
  text-align: center;
}
.rgt {
  text-align: right;
}
.money {
  font-weight: 600;
}
.goods {
  display: flex;
  gap: 12px;
  align-items: center;
  color: inherit;
  text-decoration: none;
  min-width: 0;
}
.thumb {
  width: 52px;
  height: 52px;
  border-radius: 8px;
  background: linear-gradient(135deg, #dbeafe, #e0e7ff);
  color: #3730a3;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  flex-shrink: 0;
  object-fit: cover;
}
.info {
  min-width: 0;
}
.name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cap {
  font-size: 12px;
  margin: 2px 0 4px;
}
.qty {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.step {
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  line-height: 1;
}
.step:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.del:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
h3 {
  font-size: 15px;
  margin: 0 0 12px;
}
.addrlist {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.addr {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  margin: 0;
  cursor: pointer;
}
.addr.on {
  border-color: var(--brand);
  background: #f8faff;
}
.addr .who {
  color: var(--text);
  font-size: 13px;
  white-space: nowrap;
}
.addr .detail {
  font-size: 13px;
}
.form {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed var(--line);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.grid label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin: 0;
}
.grid input {
  width: 100%;
  margin-top: 4px;
}
.settle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 16px;
  position: sticky;
  bottom: 16px;
  box-shadow: 0 2px 12px rgba(15, 23, 42, 0.08);
}
.acts {
  display: flex;
  align-items: center;
  gap: 10px;
}
.total {
  color: #b91c1c;
  font-size: 20px;
}
.big {
  padding: 11px 26px;
}
.tiny {
  font-size: 12px;
  margin: 10px 2px 0;
}
.link {
  color: var(--brand);
}
.ok {
  color: var(--ok);
  font-size: 13px;
}
</style>
