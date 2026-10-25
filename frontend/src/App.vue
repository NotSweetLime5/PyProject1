<script setup>
import { RouterLink, RouterView } from 'vue-router'
import { useAuth } from './store'

const auth = useAuth()
</script>

<template>
  <header class="bar">
    <div class="brand">
      <span class="dot" />
      AI 客服
      <small>FastAPI · pgvector · LangChain</small>
    </div>
    <nav>
      <RouterLink to="/search">商品搜索</RouterLink>
      <RouterLink to="/cart">购物车</RouterLink>
      <RouterLink to="/orders">我的订单</RouterLink>
      <RouterLink to="/chat">在线客服</RouterLink>
    </nav>
    <div class="who">
      <template v-if="auth.logged">
        <span>{{ auth.user.nick_name || auth.user.username }}</span>
        <button class="ghost" @click="auth.logout()">退出</button>
      </template>
      <RouterLink v-else to="/login" class="login-link">登录 / 注册</RouterLink>
    </div>
  </header>

  <main class="page">
    <RouterView />
  </main>
</template>
