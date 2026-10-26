<script setup>
import { reactive, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({ userId: Number, disabled: Boolean })
const labels = { brand_preference: '品牌偏好', usual_use: '常用用途', answer_style: '回答风格' }
const values = reactive({ brand_preference: '', usual_use: '', answer_style: '' })
const stored = ref({})
const lessons = ref([])
const lessonStatus = { candidate: '待确认', active: '已启用', dismissed: '已停用' }
const error = ref('')
const notice = ref('')
const saving = ref(false)

async function refresh() {
  const owner = props.userId
  if (!owner) return
  try {
    const [rows, lessonRows] = await Promise.all([api.memories(), api.memoryLessons()])
    if (props.userId !== owner) return
    const previous = stored.value
    stored.value = Object.fromEntries(rows.map(r => [r.key, r.value]))
    lessons.value = lessonRows
    for (const key of Object.keys(labels)) {
      if (values[key] === (previous[key] || '') || values[key].trim() === stored.value[key]) {
        values[key] = stored.value[key] || ''
      }
    }
    error.value = ''
  } catch (e) {
    if (props.userId === owner) error.value = e.message || '偏好加载失败'
  }
}

async function update(key, remove = false) {
  if (saving.value || props.disabled) return
  const owner = props.userId
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    if (remove) await api.deleteMemory(key)
    else await api.saveMemory(key, values[key].trim())
    if (props.userId !== owner) return
    notice.value = remove ? '已删除，下次对话不再使用这项偏好。' : '已保存，新对话也会使用这项偏好。'
    await refresh()
  } catch (e) {
    if (props.userId === owner) error.value = e.message || '偏好更新失败'
  } finally {
    saving.value = false
  }
}

async function reviewLesson(lesson, status) {
  if (saving.value || props.disabled) return
  const owner = props.userId
  saving.value = true
  error.value = notice.value = ''
  try {
    await api.reviewMemoryLesson(lesson.id, status, lesson.revision)
    if (props.userId !== owner) return
    await refresh()
    notice.value = status === 'active' ? '已启用，后续相关咨询会参考这条提醒。' : '已停用这条提醒。'
  } catch (e) {
    if (props.userId === owner) {
      // 再次触发事件也会增加版本号；刷新后保留冲突提示，供用户重新确认。
      if (e.code === 409) await refresh()
      error.value = e.message || '提醒更新失败'
    }
  } finally {
    saving.value = false
  }
}

watch(() => props.userId, () => {
  stored.value = {}
  lessons.value = []
  for (const key of Object.keys(labels)) values[key] = ''
  error.value = notice.value = ''
  refresh()
}, { immediate: true })
defineExpose({ refresh })
</script>

<template>
  <details class="memory-panel">
    <summary>长期偏好 · {{ Object.keys(stored).length }} 项</summary>
    <p>保存后可跨会话使用；本次要求优先。也可以说「记住：品牌偏好=联想」。</p>
    <p>品牌填写名称或列表；用途可填「出差办公、学习、编程、视频剪辑、游戏、拍照」等场景组合；回答风格可填「简洁、详细、通俗、分点说明」。偏好设置与业务问题请分句表达。</p>
    <div v-for="(label, key) in labels" :key="key" class="memory-row">
      <label :for="`memory-${key}`">{{ label }}</label>
      <input :id="`memory-${key}`" v-model="values[key]" maxlength="120" :disabled="saving || disabled" :placeholder="`填写${label}`" />
      <button class="ghost" :disabled="saving || disabled || !values[key].trim() || values[key].trim() === stored[key]" @click="update(key)">保存</button>
      <button class="ghost" :disabled="saving || disabled || !stored[key]" @click="update(key, true)">删除</button>
    </div>
    <section v-if="lessons.length" class="lessons" aria-label="纠错提醒">
      <h3>纠错提醒</h3>
      <p>系统发现的潜在错误先列为待确认；启用后，仅在相关咨询中参考。</p>
      <article v-for="lesson in lessons" :key="lesson.id">
        <strong>{{ lesson.title }}</strong>
        <span class="lesson-status">{{ lessonStatus[lesson.status] }} · {{ lesson.occurrences }} 次记录</span>
        <p>{{ lesson.guidance }}</p>
        <button class="ghost" :disabled="saving || disabled || lesson.status === 'active'" @click="reviewLesson(lesson, 'active')">启用提醒</button>
        <button class="ghost" :disabled="saving || disabled || lesson.status === 'dismissed'" @click="reviewLesson(lesson, 'dismissed')">停用</button>
      </article>
    </section>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" class="err" role="alert">{{ error }}</p>
  </details>
</template>

<style scoped>
.memory-panel { padding: 10px 18px; border-bottom: 1px solid var(--line); overflow-y: auto; max-height: 210px; flex-shrink: 0; }
summary { cursor: pointer; font-size: 13px; }
p { font-size: 12px; color: var(--muted); margin: 8px 0; }
.memory-row { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
label { min-width: 58px; font-size: 12px; }
input { flex: 1; min-width: 0; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; }
button { padding: 5px 8px; font-size: 12px; }
.err { color: #b91c1c; }
.lessons { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 4px; }
h3 { font-size: 13px; margin: 8px 0; }
article { padding: 6px 0; font-size: 12px; }
.lesson-status { margin-left: 8px; color: var(--muted); }
.lessons button + button { margin-left: 6px; }
</style>
