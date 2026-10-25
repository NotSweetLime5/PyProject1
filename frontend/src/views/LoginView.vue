<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api'
import { useAuth } from '../store'

const auth = useAuth()
const router = useRouter()
const route = useRoute()
const mode = ref('login')
const busy = ref(false)
const error = ref('')
const form = reactive({ username: '', password: '', confirm: '', phone: '', nick_name: '' })

async function submit() {
  error.value = ''
  busy.value = true
  try {
    if (mode.value === 'register') {
      // confirm_password 只在这里比：它的作用是防止掩码输入框里打错字，
      // 提交之后这个字段就再无任何意义，所以不发给后端
      if (form.password !== form.confirm) {
        throw new ApiError('两次输入的密码不一致')
      }
      await auth.register({
        username: form.username,
        password: form.password,
        phone: form.phone,
        nick_name: form.nick_name || undefined,
      })
    } else {
      await auth.login(form.username, form.password)
    }
    // 从详情页被弹回来登录的，登完要回到原来那一页，而不是统统丢去客服页
    router.push(String(route.query.next || '/chat'))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '请求失败，请检查后端是否在运行'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="card box">
    <div class="tabs">
      <button :class="{ on: mode === 'login' }" @click="mode = 'login'; error = ''">登录</button>
      <button :class="{ on: mode === 'register' }" @click="mode = 'register'; error = ''">注册</button>
    </div>

    <form @submit.prevent="submit">
      <label>
        用户名
        <input v-model="form.username" required :placeholder="mode === 'register' ? '4-50 位字母数字下划线' : ''" />
      </label>
      <label v-if="mode === 'register'">
        手机号
        <input v-model="form.phone" required placeholder="11 位大陆手机号" maxlength="11" />
      </label>
      <label>
        密码
        <input v-model="form.password" type="password" required :placeholder="mode === 'register' ? '8-24 位' : ''" />
      </label>
      <label v-if="mode === 'register'">
        确认密码
        <input v-model="form.confirm" type="password" required />
      </label>
      <label v-if="mode === 'register'">
        昵称（可选）
        <input v-model="form.nick_name" />
      </label>

      <p v-if="error" class="err">{{ error }}</p>
      <button class="primary full" type="submit" :disabled="busy">
        {{ busy ? '提交中…' : mode === 'login' ? '登录' : '注册并登录' }}
      </button>
    </form>

    <p class="tip muted">
      密码用 bcrypt(cost=12) 存储，不是原项目的 MD5；登录接口有按 IP 的限流。
    </p>
  </section>
</template>

<style scoped>
.box {
  max-width: 400px;
  margin: 40px auto;
}
.tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 18px;
}
.tabs button {
  flex: 1;
  padding: 9px;
  border: 1px solid var(--line);
  background: #f8fafc;
  border-radius: 8px;
  color: var(--muted);
}
.tabs button.on {
  background: #eef2ff;
  border-color: #c7d2fe;
  color: var(--brand);
  font-weight: 600;
}
label {
  display: block;
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--muted);
}
input {
  width: 100%;
  margin-top: 5px;
}
.full {
  width: 100%;
  margin-top: 6px;
}
.tip {
  font-size: 12px;
  margin: 14px 0 0;
}
</style>
