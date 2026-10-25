import { defineStore } from 'pinia'
import { api, getToken, setToken } from './api'

export const useAuth = defineStore('auth', {
  state: () => ({
    token: getToken(),
    user: JSON.parse(localStorage.getItem('user') || 'null'),
  }),
  getters: {
    logged: (s) => Boolean(s.token && s.user),
  },
  actions: {
    _persist() {
      localStorage.setItem('user', JSON.stringify(this.user))
    },
    async login(username, password) {
      const data = await api.login({ username, password })
      this.token = data.access_token
      this.user = data.user
      setToken(data.access_token)
      this._persist()
      return data
    },
    async register(payload) {
      await api.register(payload)
      // 注册成功后直接登录，省一次用户输入
      return this.login(payload.username, payload.password)
    },
    logout() {
      // JWT 无状态，服务端没有可吊销的东西；前端删掉 token 就是本项目的"登出"。
      // 真正的强制下线要 Redis 黑名单，见 README 的「已知取舍」。
      this.token = null
      this.user = null
      setToken(null)
      localStorage.removeItem('user')
      // 客服会话凭据也一起丢：那个 sid 现在归 user 所有，游客身份带它去提问
      // 只会换来一次归属失败。会话本身还在服务端，登录后 chatSessions 会把它捞回来。
      sessionStorage.removeItem('chat_sid')
      sessionStorage.removeItem('chat_secret')
    },
    /** token 过期时后端会返回 401，这时必须清干净，否则界面停在"已登录"的假象里 */
    handleAuthFailure() {
      if (this.token) this.logout()
    },
  },
})
