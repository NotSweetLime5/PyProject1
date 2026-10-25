<script setup>
import { computed, ref, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '../api'
import { useAuth } from '../store'

const route = useRoute()
const router = useRouter()
const auth = useAuth()

const goods = ref(null)
const loading = ref(true)
const error = ref('')
const shot = ref(0)
const added = ref('')

// 规格值可能是 JSON 对象，也可能是 NULL（源字段可空），统一成可遍历的数组
const specs = computed(() =>
  Object.entries(goods.value?.spec_values || {}).map(([k, v]) => ({ k, v })),
)

async function load(id) {
  loading.value = true
  error.value = ''
  added.value = ''
  shot.value = 0
  try {
    goods.value = await api.goodsDetail(id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '加载失败'
    goods.value = null
  } finally {
    loading.value = false
  }
}

async function addToCart() {
  added.value = ''
  if (!auth.logged) {
    // 购物车接口要鉴权，未登录点了就该引导去登录，而不是发一个必然 401 的请求
    router.push({ path: '/login', query: { next: route.fullPath } })
    return
  }
  try {
    await api.addToCart(goods.value.id, 1)
    added.value = '已加入购物车'
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '加入失败'
  }
}

// id 变化时重新加载：从详情页跳另一个详情页（如推荐位）不该白屏
watchEffect(() => {
  const id = Number(route.params.id)
  if (id) load(id)
})
</script>

<template>
  <div>
    <RouterLink to="/search" class="back">← 返回搜索</RouterLink>

    <p v-if="loading" class="card muted pad">加载中…</p>
    <p v-else-if="error" class="card err pad">{{ error }}</p>

    <template v-else-if="goods">
      <nav class="crumb muted">
        <span v-for="(c, i) in goods.category_names" :key="c">
          <span v-if="i"> › </span>{{ c }}
        </span>
      </nav>

      <section class="card top">
        <div class="gallery">
          <img
            v-if="goods.images[shot]"
            :src="goods.images[shot].url"
            :alt="goods.goods_name"
            class="main"
          />
          <div v-else class="main noimg">暂无图片</div>
          <div v-if="goods.images.length > 1" class="thumbs">
            <button
              v-for="(im, i) in goods.images"
              :key="im.id"
              :class="{ on: i === shot }"
              @click="shot = i"
            >
              <img :src="im.url" :alt="`图 ${i + 1}`" />
            </button>
          </div>
        </div>

        <div class="meta">
          <h1>{{ goods.goods_name }}</h1>
          <p class="muted cap">{{ goods.caption }}</p>
          <p v-if="goods.model_name" class="muted">型号：{{ goods.model_name }}</p>
          <div class="price">¥{{ goods.price }}</div>
          <div class="badges">
            <span class="tag">{{ goods.brand_name || '无品牌' }}</span>
            <span v-if="!goods.is_marketable" class="tag">已下架</span>
            <span v-else-if="goods.stock > 20" class="tag fulltext">现货充足</span>
            <span v-else-if="goods.stock > 0" class="tag fuzzy">库存紧张</span>
            <span v-else class="tag">无货</span>
          </div>

          <dl v-if="specs.length">
            <template v-for="s in specs" :key="s.k">
              <dt>{{ s.k }}</dt>
              <dd>{{ s.v }}</dd>
            </template>
          </dl>

          <div class="acts">
            <button class="primary" :disabled="!goods.is_marketable || goods.stock === 0" @click="addToCart">
              {{ goods.is_marketable && goods.stock ? '加入购物车' : '暂不可购' }}
            </button>
            <span v-if="added" class="ok">{{ added }}</span>
          </div>
          <p v-if="!auth.logged" class="muted tip">登录后可以下单，并让 AI 客服帮你查订单状态。</p>
        </div>
      </section>

      <section v-if="goods.introduction" class="card intro">
        <h2>商品介绍</h2>
        <p>{{ goods.introduction }}</p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.back {
  display: inline-block;
  color: var(--muted);
  text-decoration: none;
  margin-bottom: 12px;
  font-size: 13px;
}
.pad {
  padding: 24px;
}
.crumb {
  font-size: 13px;
  margin-bottom: 10px;
}
.top {
  display: flex;
  gap: 28px;
  flex-wrap: wrap;
}
.gallery {
  width: 340px;
  flex-shrink: 0;
}
.main {
  width: 340px;
  height: 340px;
  border-radius: 10px;
  object-fit: cover;
  background: #f1f5f9;
}
.noimg {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
}
.thumbs {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.thumbs button {
  width: 62px;
  height: 62px;
  padding: 0;
  border: 2px solid var(--line);
  border-radius: 8px;
  background: none;
  overflow: hidden;
}
.thumbs button.on {
  border-color: var(--brand);
}
.thumbs img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.meta {
  flex: 1;
  min-width: 260px;
}
h1 {
  font-size: 21px;
  margin: 0 0 6px;
}
.cap {
  margin: 0 0 14px;
}
.price {
  font-size: 28px;
  font-weight: 700;
  color: #b91c1c;
}
.badges {
  display: flex;
  gap: 6px;
  margin: 10px 0 16px;
}
dl {
  display: grid;
  grid-template-columns: 84px 1fr;
  gap: 8px 12px;
  margin: 0 0 20px;
  padding: 14px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
dt {
  color: var(--muted);
  font-size: 13px;
}
dd {
  margin: 0;
}
.acts {
  display: flex;
  align-items: center;
  gap: 12px;
}
.ok {
  color: var(--ok);
  font-size: 13px;
}
.tip {
  font-size: 12px;
  margin: 10px 0 0;
}
.intro h2 {
  font-size: 15px;
  margin: 0 0 8px;
}
.intro p {
  margin: 0;
  color: #374151;
  white-space: pre-wrap;
}
</style>
