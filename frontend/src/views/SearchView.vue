<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError, api } from '../api'

const router = useRouter()
const q = ref('')
const loading = ref(false)
const error = ref('')
const result = ref(null)
const semantic = ref(true)

async function run() {
  const term = q.value.trim()
  if (!term) return
  loading.value = true
  error.value = ''
  try {
    result.value = await api.search(term, { semantic: semantic.value ? 'true' : 'false', limit: 20 })
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '请求失败'
    result.value = null
  } finally {
    loading.value = false
  }
}

</script>

<template>
  <section class="card">
    <h2 style="margin: 0 0 4px">商品搜索</h2>
    <p class="muted" style="margin: 0 0 16px">
      支持完整型号、商品关键词和自然语言需求搜索
    </p>

    <div class="row">
      <input
        v-model="q"
        placeholder="试试「Mate60Pro」「降噪耳机」「拍照清晰的手机」"
        style="flex: 1; min-width: 240px"
        @keyup.enter="run"
      />
      <button class="primary" :disabled="loading || !q.trim()" @click="run">
        {{ loading ? '搜索中…' : '搜索' }}
      </button>
      <label class="switch">
        <input v-model="semantic" type="checkbox" />
        语义检索
      </label>
    </div>
    <p v-if="error" class="err">{{ error }}</p>
  </section>

  <section v-if="result" class="card" style="margin-top: 16px">
    <div class="dbg">
      <span class="muted">分词结果</span>
      <code>{{ result.segmented || '（空）' }}</code>
      <span class="muted" style="margin-left: auto">命中 {{ result.total }} 条</span>
    </div>

    <p v-if="!result.items.length" class="muted" style="padding: 24px 0; text-align: center">
      没有找到相关商品。关掉「语义检索」再试一次可以对比出是哪一路在起作用。
    </p>

    <ul class="list">
      <li v-for="it in result.items" :key="it.id">
        <RouterLink :to="`/goods/${it.id}`" class="thumblink">
          <img v-if="it.header_pic" :src="it.header_pic" :alt="it.goods_name" class="thumb" />
          <div v-else class="thumb">{{ it.goods_name.slice(0, 2) }}</div>
        </RouterLink>
        <div class="info">
          <div class="name">
            <RouterLink :to="`/goods/${it.id}`">{{ it.goods_name }}</RouterLink>
            <span v-if="!it.is_marketable" class="tag">已下架</span>
          </div>
          <div class="cap muted">{{ it.caption }}</div>
          <div v-if="it.model_name" class="cap muted">型号：{{ it.model_name }}</div>
          <div class="tags">
            <span v-for="s in it.matched_by" :key="s" class="tag" :class="s">{{ s === 'model_exact' ? '型号精确匹配' : s }}</span>
          </div>
        </div>
        <div class="right">
          <div class="price">¥{{ it.price }}</div>
          <div class="muted small">库存 {{ it.stock }}</div>
          <button class="ghost" @click="router.push(`/goods/${it.id}`)">详情</button>
        </div>
      </li>
    </ul>

  </section>
</template>

<style scoped>
.row {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.switch {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  user-select: none;
}
.dbg {
  display: flex;
  align-items: center;
  gap: 10px;
  background: #f8fafc;
  border: 1px dashed var(--line);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 14px;
  font-size: 13px;
}
.dbg code {
  color: #0f172a;
  word-break: break-all;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.list li {
  display: flex;
  gap: 14px;
  align-items: center;
  padding: 14px 0;
  border-top: 1px solid var(--line);
}
.thumb {
  width: 46px;
  height: 46px;
  border-radius: 8px;
  background: linear-gradient(135deg, #dbeafe, #e0e7ff);
  color: #3730a3;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  object-fit: cover;
}
.info {
  flex: 1;
  min-width: 0;
}
.name {
  font-weight: 600;
}
.name a {
  color: inherit;
  text-decoration: none;
}
.name a:hover {
  color: var(--brand);
}
.thumblink {
  flex-shrink: 0;
}
.cap {
  font-size: 12px;
  margin: 2px 0 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.right {
  text-align: right;
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-end;
}
.price {
  font-weight: 700;
  color: #b91c1c;
}
.small {
  font-size: 12px;
}.intro {
  color: var(--muted);
  font-size: 13px;
  background: #f8fafc;
  padding: 10px 12px;
  border-radius: 8px;
}
</style>
