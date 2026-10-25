import { createPinia } from 'pinia'
import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'

import App from './App.vue'
import { getToken } from './api'
import './styles.css'
import CartView from './views/CartView.vue'
import ChatView from './views/ChatView.vue'
import GoodsDetailView from './views/GoodsDetailView.vue'
import LoginView from './views/LoginView.vue'
import OrdersView from './views/OrdersView.vue'
import SearchView from './views/SearchView.vue'

const router = createRouter({
  // hash 模式：静态托管时不需要为 SPA 重写路由规则，少一个部署坑
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/search' },
    { path: '/search', component: SearchView, meta: { title: '商品搜索' } },
    { path: '/chat', component: ChatView, meta: { title: 'AI 客服' } },
    { path: '/goods/:id', component: GoodsDetailView, meta: { title: '商品详情' } },
    { path: '/cart', component: CartView, meta: { title: '购物车', auth: true } },
    { path: '/orders', component: OrdersView, meta: { title: '我的订单', auth: true } },
    { path: '/login', component: LoginView, meta: { title: '登录', public: true } },
  ],
})

router.beforeEach((to) => {
  document.title = `${to.meta.title ?? ''} · AI 客服`
  // 守卫只挡"进页面"，不挡页面内的按钮：购物车/订单页里所有接口本来就要鉴权，
  // 真正的强制在服务端，这里只是不让用户看到一个必然全报错的空壳页
  if (to.meta.auth && !getToken()) {
    return { path: '/login', query: { next: to.fullPath } }
  }
})

createApp(App).use(createPinia()).use(router).mount('#app')
