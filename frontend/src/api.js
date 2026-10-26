const BASE = '/api/v1'

export function getToken() {
  return localStorage.getItem('token')
}
export function setToken(t) {
  if (t) localStorage.setItem('token', t)
  else localStorage.removeItem('token')
}

export class ApiError extends Error {
  constructor(msg, code) {
    super(msg)
    this.code = code
  }
}

function authHeaders(extra = {}) {
  const h = { 'Content-Type': 'application/json', ...extra }
  const token = getToken()
  if (token) h.Authorization = `Bearer ${token}`
  return h
}

/**
 * 所有非流式请求走这里。后端统一返回 {code,msg,data}，
 * 所以"成功与否"判断集中在这一个函数里，业务代码只管拿 data。
 */
export async function http(path, { method = 'GET', body, params, headers } = {}) {
  const url = new URL(BASE + path, location.origin)
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v)
    }
  }
  const res = await fetch(url, {
    method,
    headers: authHeaders(headers || {}),
    body: body ? JSON.stringify(body) : undefined,
  })
  let payload
  try {
    payload = await res.json()
  } catch {
    throw new ApiError(`服务无响应（HTTP ${res.status}）`, res.status)
  }
  if (!payload || payload.code !== 200) {
    throw new ApiError(payload?.msg || `HTTP ${res.status}`, payload?.code ?? res.status)
  }
  return payload.data
}

/**
 * SSE 流式对话。
 *
 * 不用 EventSource：它不能带 Authorization 头，而我们的接口需要鉴权。
 * 所以用 fetch + ReadableStream 自己拆帧 —— 代价是要手工处理
 * "一个 chunk 里含多帧"和"一帧被拆到两个 chunk"这两种情况。
 *
 * session 是 {sessionId, guestSecret}，都可以是空：空的 session_id 让服务端签发，
 * 结果通过首帧 `session` 回来（见 onSession）。游客的 guestSecret 只在签发那一次
 * 出现在帧里，之后由调用方带回来。
 */
export async function askStream(message, session = {}, handlers = {}, signal) {
  const res = await fetch(`${BASE}/chat/stream`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({
      message,
      session_id: session.sessionId || undefined,
      guest_secret: session.guestSecret || undefined,
    }),
    signal,
  })

  // 限流(701)、鉴权失败和会话失效(410)是在 StreamingResponse 建立之前抛的，
  // 所以这些错误走的是普通 JSON 响应，不是 SSE 帧
  if (!res.ok || !(res.headers.get('content-type') || '').includes('text/event-stream')) {
    const payload = await res.json().catch(() => null)
    throw new ApiError(payload?.msg || `HTTP ${res.status}`, payload?.code ?? res.status)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  const dispatch = (block) => {
    const ev = /^event: (.+)$/m.exec(block)?.[1]
    const raw = /^data: (.+)$/m.exec(block)?.[1]
    if (!ev || raw === undefined) return
    let data
    try {
      data = JSON.parse(raw)
    } catch {
      return
    }
    if (ev === 'token') handlers.onToken?.(data)
    else if (ev === 'status') handlers.onStatus?.(data)
    else if (ev === 'refs') handlers.onRefs?.(data)
    else if (ev === 'session') handlers.onSession?.(data)
    else if (ev === 'done') handlers.onDone?.(data)
    else if (ev === 'error') handlers.onError?.(data)
    else if (ev === 'memory') handlers.onMemory?.(data)
  }

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    // 帧之间用空行分隔；缓冲区里可能同时躺着好几帧，也可能只有半帧
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      dispatch(buf.slice(0, idx))
      buf = buf.slice(idx + 2)
    }
  }
  if (buf.trim()) dispatch(buf)
}

export const api = {
  // 认证
  register: (body) => http('/auth/register', { method: 'POST', body }),
  login: (body) => http('/auth/login', { method: 'POST', body }),

  // 商品与检索
  search: (q, opts = {}) => http('/search', { params: { q, ...opts } }),
  goodsDetail: (id) => http(`/goods/${id}`),

  // 购物车。四个写接口都返回"改动后的整张购物车"，所以调用方不用再拉一次
  cart: () => http('/cart'),
  addToCart: (goodsId, quantity = 1) =>
    http('/cart', { method: 'POST', body: { goods_id: goodsId, quantity } }),
  setCartQty: (itemId, quantity) =>
    http(`/cart/${itemId}`, { method: 'PUT', body: { quantity } }),
  removeCart: (itemIds) => http('/cart/remove', { method: 'POST', body: { item_ids: itemIds } }),

  // 收货地址
  addresses: () => http('/addresses'),
  addAddress: (body) => http('/addresses', { method: 'POST', body }),

  // 订单
  orders: (status) => http('/orders', { params: status ? { status } : {} }),
  orderDetail: (id) => http(`/orders/${id}`),
  createOrder: (body, idemKey) =>
    http('/orders', { method: 'POST', body, headers: idemKey }),
  payOrder: (id) => http(`/orders/${id}/pay`, { method: 'POST' }),
  cancelOrder: (id) => http(`/orders/${id}/cancel`, { method: 'POST' }),

  // 客服
  chatHistory: (sessionId, guestSecret) =>
    http('/chat/history', { params: { session_id: sessionId, guest_secret: guestSecret } }),
  chatSessions: () => http('/chat/sessions'),
  memories: () => http('/chat/memories'),
  saveMemory: (key, value) => http(`/chat/memories/${key}`, { method: 'PUT', body: { value } }),
  deleteMemory: (key) => http(`/chat/memories/${key}`, { method: 'DELETE' }),
  memoryLessons: () => http('/chat/lessons'),
  reviewMemoryLesson: (id, status, revision) =>
    http(`/chat/lessons/${id}`, { method: 'PUT', body: { status, revision } }),
  claimSession: (sessionId, guestSecret) =>
    http('/chat/session/claim', { method: 'POST', body: { session_id: sessionId, guest_secret: guestSecret } }),
}
