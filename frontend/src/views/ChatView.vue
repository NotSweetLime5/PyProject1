<script setup>
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, askStream, ApiError } from '../api'
import { useAuth } from '../store'
import MemoryPanel from '../components/MemoryPanel.vue'

const auth = useAuth()
const msgs = ref([])
const input = ref('')
const busy = ref(false)
const status = ref('')
const error = ref('')
const scroller = ref(null)
// error 会被 boot() 清掉；notice 专门留那些"登出之后仍然要让用户看见"的原因
const notice = ref('')
let controller = null
let viewRevision = 0

const sid = ref('')
const secret = ref('')
const sessions = ref([])
const memoryPanel = ref(null)

/**
 * 会话凭据只放 sessionStorage：新标签页 = 新会话，关掉标签页不承诺能续。
 * 这正是游客要的粒度 —— 归属由服务端 chat_sessions 判定，浏览器只保管凭据，
 * 不再自己造 sid（旧版用 crypto.randomUUID 造，非安全上下文里还会直接抛）。
 */
const SID_KEY = 'chat_sid'
const SECRET_KEY = 'chat_secret'

function readLocal() {
  sid.value = sessionStorage.getItem(SID_KEY) || ''
  secret.value = sessionStorage.getItem(SECRET_KEY) || ''
}

function writeLocal(newSid, newSecret) {
  if (newSid) sessionStorage.setItem(SID_KEY, newSid)
  if (newSecret) sessionStorage.setItem(SECRET_KEY, newSecret)
  // guestSecret 只在签发那一帧非空；之后帧里是 null，不能拿 null 去覆盖已存的那份
  sid.value = newSid || sid.value
  secret.value = newSecret || secret.value
}

function clearLocal() {
  sessionStorage.removeItem(SID_KEY)
  sessionStorage.removeItem(SECRET_KEY)
  sid.value = ''
  secret.value = ''
}

const GREET_GUEST =
  '你好，我是百展商城的在线客服。可以问我商品推荐、价格库存、发货退换货这些平台规则。\n\n未登录状态下我查不到你的订单 —— 登录后可以问「我的订单到哪了」。'

/**
 * 开场白分两种：游客要先知道「查不到订单」这件事，
 * 登录用户没这句废话，直接给可问的例子。
 */
function greeting() {
  if (!auth.logged) return GREET_GUEST
  const name = auth.user.nick_name || auth.user.username
  return `${name}，你好。我可以查商品、解释平台规则，也能帮你查订单状态。\n\n直接问就行，例如「我上次买的手机保修到什么时候」。`
}

/**
 * 会话归属：登录用户按 user 走，游客每次都是全新的。
 *
 * 之前把 session_id 存 localStorage，导致两个都不对：游客下次进来还在上次那个会话里，
 * 而登录后又不会把他名下的历史会话拉回来 —— 因为会话锚在浏览器而不是锚在用户。
 * 现在登录后进入"该用户最近的一次会话"，游客不写任何持久化状态。
 */
async function boot() {
  const revision = ++viewRevision
  controller?.abort()
  error.value = ''
  // 重新登录成功后，上一轮"登录已过期"的提示就没意义了；
  // 但登出触发的 boot() 不能清，否则用户只看到界面突然变成游客版
  if (auth.logged) notice.value = ''
  readLocal()

  if (!auth.logged) {
    // 游客：sid 可能为空（本标签页第一次进来），空就让它由服务端签发
    sessions.value = []
    msgs.value = [{ role: 'assistant', content: greeting(), refs: null }]
    if (sid.value) {
      // 同一标签页里刷新过页面：把这次停留期间的对话捞回来
      try {
        const history = await api.chatHistory(sid.value, secret.value)
        if (revision !== viewRevision) return
        if (history.length) {
          msgs.value = history.map((h) => ({
            role: h.role,
            content: h.content,
            refs: h.refs && Array.isArray(h.refs) ? h.refs : null,
          }))
        }
      } catch (e) {
        if (revision !== viewRevision) return
        if (e instanceof ApiError && e.code === 410) {
          // 本地那份已经失效：现在不清，就要等用户发下一条消息再撞一次 410，
          // 白挨一次"会话已失效"的提示。清掉之后下一条直接签发新的。
          clearLocal()
        }
        // 其余异常（后端没起来等）继续用空会话，不拦住游客提问
      }
    }
    return
  }

  try {
    // 登录前在这个标签页聊过的话，先认领到自己名下，"继续上次会话"才捞得到它。
    // 认领失败（会话已属他人/凭据过期）就丢回全新会话，不猜、不静默改写。
    if (sid.value && secret.value) {
      try {
        await api.claimSession(sid.value, secret.value)
        if (revision !== viewRevision) return
        sessionStorage.removeItem(SECRET_KEY)
        secret.value = ''
      } catch (e) {
        if (revision !== viewRevision) return
        if (e instanceof ApiError && e.code === 410) clearLocal()
        else throw e
      }
    }
    const fetchedSessions = await api.chatSessions()
    if (revision !== viewRevision) return
    const selected = fetchedSessions.find(s => s.session_id === sid.value)?.session_id || fetchedSessions[0]?.session_id || ''
    const history = selected ? await api.chatHistory(selected) : []
    if (revision !== viewRevision) return
    sessions.value = fetchedSessions
    clearLocal()
    if (selected) writeLocal(selected, '')
    msgs.value = history.map((h) => ({
      role: h.role,
      content: h.content,
      refs: h.refs && Array.isArray(h.refs) ? h.refs : null,
    }))
    if (!msgs.value.length) {
      msgs.value = [{ role: 'assistant', content: greeting(), refs: null }]
    }
  } catch (e) {
        if (revision !== viewRevision) return
    error.value = e instanceof ApiError ? e.message : '加载历史会话失败'
    // 走到这里的一定是已登录用户（游客在上面已经 return 了），
    // 所以不能给游客欢迎语，否则等于告诉已登录的人"你还没登录"
    msgs.value = [{ role: 'assistant', content: greeting(), refs: null }]
  }
  scroll()
}

const SUGGESTS = [
  '你们一般多久发货？',
  '2000 块以内有没有降噪耳机？',
  '刚买就降价了能退差价吗？',
  '帮我写一首关于秋天的诗',
]

function scroll() {
  nextTick(() => {
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
  })
}

async function send(text) {
  const message = (text ?? input.value).trim()
  if (!message || busy.value) return
  input.value = ''
  error.value = ''
  status.value = ''
  msgs.value.push({ role: 'user', content: message, refs: null })
  const bot = { role: 'assistant', content: '', refs: null, streaming: true }
  msgs.value.push(bot)
  scroll()

  busy.value = true
  controller = new AbortController()
  let aborted = false
  const owner = auth.user?.id
  const revision = viewRevision
  try {
    await askStream(
      message,
      { sessionId: sid.value, guestSecret: secret.value },
      {
        onSession: (s) => {
          // 签发结果只在这一帧里给全（含 secret 明文），错过就永久丢了
          if (revision === viewRevision && auth.user?.id === owner && s.session_id) writeLocal(s.session_id, s.guest_secret)
        },
        onStatus: (t) => {
          if (revision !== viewRevision) return
          status.value = t
        },
        onToken: (t) => {
          if (revision !== viewRevision) return
          status.value = ''
          bot.content += t
          scroll()
        },
        onRefs: (r) => {
          if (revision !== viewRevision) return
          bot.refs = r
        },
        onError: (e) => {
          if (revision !== viewRevision) return
          error.value = e.msg || '客服出错了'
        },
        onMemory: (e) => {
          if (revision !== viewRevision || auth.user?.id !== owner) return
          const parts = []
          if (e.updated?.length) parts.push(`已记住：${e.updated.join('、')}`)
          if (e.deleted?.length) parts.push(`已删除：${e.deleted.join('、')}`)
          notice.value = parts.join('；')
        },
      },
      controller.signal,
    )
  } catch (e) {
    aborted = e?.name === 'AbortError'
    if (revision !== viewRevision || auth.user?.id !== owner) return
    if (!aborted) {
      error.value = e instanceof ApiError ? e.message : '连接失败'
      if (e instanceof ApiError && e.code === 410) {
        // 归属校验没过（服务端已强制，或本地那份 sid 是旧的）。
        // 丢回全新会话并把问题还给输入框，让用户决定要不要再问一次 ——
        // 不自动重发：那一轮的上下文已经没了，重发的答案会答错东西。
        clearLocal()
        input.value = message
        notice.value = '这个会话已失效，已为你重开一个新的。上面的问题请再发一次。'
      } else if (e instanceof ApiError && e.code === 401) {
        notice.value = '登录已过期，这条会话的历史不再可见。重新登录后可以接着上次继续。'
        auth.handleAuthFailure()
      }
    }
  } finally {
    bot.streaming = false
    // 兜底必须放在 finally：askStream 抛异常时走的是 catch，
    // 放 try 里的话空气泡会永远留在列表上（这就是那个 bug）
    if (!bot.content) {
      bot.content = aborted ? '（已停止）' : '（没有收到回答）'
    }
    status.value = ''
    busy.value = false
    controller = null
    if (revision === viewRevision && auth.logged && auth.user?.id === owner) {
      await memoryPanel.value?.refresh()
      try {
        const refreshed = await api.chatSessions()
        if (revision === viewRevision) sessions.value = refreshed
      } catch { /* 下次进入时重试 */ }
    }
    scroll()
  }
}

async function openSession(event) {
  if (busy.value) return
  const selected = event.target.value
  if (!selected) return
  const revision = ++viewRevision
  busy.value = true
  try {
    const history = await api.chatHistory(selected)
    if (revision !== viewRevision) return
    clearLocal()
    writeLocal(selected, '')
    msgs.value = history.map(h => ({ role: h.role, content: h.content, refs: h.refs }))
    error.value = notice.value = ''
    scroll()
  } catch (e) {
    if (revision === viewRevision) error.value = e.message || '加载会话失败'
  } finally {
    busy.value = false
  }
}

function stop() {
  controller?.abort()
}

/**
 * 只有这里才创建新 conversation。
 * 进入页面时不会走到这条路径 —— 登录用户是"继续最近一次会话"，
 * 游客是"这次停留期间的临时会话"，两者都不该被当成用户主动新建。
 * 旧会话不删：session_id 换成新值，chat_messages 里旧的那些原样留着。
 */
async function newConversation() {
  if (busy.value) return
  const revision = ++viewRevision
  // 不再生成 sid：清空本地凭据，下一条消息由服务端签发一个新的。
  // secret 必须一起清，否则会把上一个会话的凭据带给新 sid，白得一个归属失败。
  clearLocal()
  msgs.value = [{ role: 'assistant', content: greeting(), refs: null }]
  error.value = ''
  notice.value = ''
  if (auth.logged) {
    // 让"最近一次活跃"立刻反映出新会话，否则下次进入还会回到旧的那条
    try {
      const refreshed = await api.chatSessions()
      if (revision === viewRevision) sessions.value = refreshed
    } catch {
      sessions.value = []
    }
  }
}

onMounted(boot)
onUnmounted(() => { ++viewRevision; controller?.abort() })

// 用户停在客服页时登录/退出，组件不会重新挂载，所以要显式重新引导一次。
// 不监听的话：登录后界面还停在游客那条临时会话上，"继续上次会话"形同虚设。
watch(() => auth.user?.id, boot)
</script>

<template>
  <section class="wrap card">
    <div class="head">
      <div>
        <strong>在线客服</strong>
        <span class="muted" style="margin-left: 8px; font-size: 12px">
          RAG + 工具调用 · 回答只依据检索到的商品与规则
        </span>
      </div>
      <button class="ghost" :disabled="busy" @click="newConversation">新对话</button>
    </div>

    <div v-if="auth.logged && sessions.length" class="session-picker">
      <label for="chat-session">历史会话</label>
      <select id="chat-session" :value="sid" :disabled="busy" @change="openSession">
        <option value="">选择会话</option>
        <option v-for="s in sessions" :key="s.session_id" :value="s.session_id">{{ s.preview || '历史对话' }}</option>
      </select>
    </div>
    <MemoryPanel v-if="auth.logged" ref="memoryPanel" :user-id="auth.user.id" :disabled="busy" />

    <div ref="scroller" class="log">
      <div v-for="(m, i) in msgs" :key="i" class="msg" :class="m.role">
        <div class="avatar">{{ m.role === 'user' ? '我' : 'AI' }}</div>
        <div class="bubble">
          <div class="body">{{ m.content }}<span v-if="m.streaming" class="caret" /></div>
          <div v-if="m.refs?.length" class="refs">
            <span class="refs-label">参考</span>
            <span v-for="(r, j) in m.refs" :key="j" class="tag" :class="r.type === 'faq' ? 'semantic' : 'fulltext'">
              <template v-if="r.type === 'faq'">规则：{{ r.question }}</template>
              <template v-else-if="r.type === 'goods'">商品：{{ r.name }} ¥{{ r.price }}</template>
              <template v-else-if="r.type === 'order'">订单：{{ r.order_no }}</template>
              <template v-else-if="r.type === 'blocked'">已拦截：{{ r.words?.join('、') }}</template>
            </span>
          </div>
        </div>
      </div>
      <div v-if="status" class="typing">{{ status }}</div>
    </div>

    <div v-if="!msgs.length" class="suggests">
      <button v-for="s in SUGGESTS" :key="s" class="ghost" @click="send(s)">{{ s }}</button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-if="notice" class="notice">{{ notice }}</p>

    <div class="input">
      <textarea
        v-model="input"
        rows="2"
        placeholder="输入问题，Enter 发送，Shift+Enter 换行"
        @keydown.enter.exact.prevent="send()"
      />
      <div class="acts">
        <button v-if="busy" class="ghost" @click="stop">停止</button>
        <button class="primary" :disabled="busy || !input.trim()" @click="send()">
          {{ busy ? '回答中…' : '发送' }}
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 130px);
  min-height: 460px;
  padding: 0;
  overflow: hidden;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
}
.session-picker { display: flex; gap: 10px; align-items: center; padding: 8px 18px; border-bottom: 1px solid var(--line); font-size: 12px; }
.session-picker select { flex: 1; min-width: 0; padding: 5px; border: 1px solid var(--line); border-radius: 6px; }
.log {
  flex: 1;
  overflow-y: auto;
  padding: 18px;
  background: #fafbfc;
}
.msg {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.msg.user {
  flex-direction: row-reverse;
}
.avatar {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  background: #e0e7ff;
  color: #3730a3;
}
.msg.user .avatar {
  background: #dcfce7;
  color: #15803d;
}
.bubble {
  max-width: 74%;
}
.body {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 10px 14px;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.user .body {
  background: #eff6ff;
  border-color: #dbeafe;
}
.caret {
  display: inline-block;
  width: 6px;
  height: 15px;
  background: var(--brand);
  vertical-align: -2px;
  animation: blink 1s steps(2) infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.refs {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
.refs-label {
  font-size: 11px;
  color: var(--muted);
}
.typing {
  color: var(--muted);
  font-size: 13px;
  padding-left: 40px;
}
.suggests {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  padding: 12px 18px 0;
}
.err {
  margin: 8px 18px 0;
}
.notice {
  margin: 8px 18px 0;
  padding: 8px 12px;
  border: 1px solid #fde68a;
  background: #fffbeb;
  border-radius: 8px;
  color: #b45309;
  font-size: 13px;
}
.input {
  display: flex;
  gap: 10px;
  padding: 14px 18px;
  border-top: 1px solid var(--line);
  background: #fff;
  align-items: flex-end;
}
textarea {
  flex: 1;
  font: inherit;
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  resize: none;
}
textarea:focus {
  outline: 2px solid #dbeafe;
  border-color: var(--brand);
}
.acts {
  display: flex;
  gap: 8px;
}
</style>
