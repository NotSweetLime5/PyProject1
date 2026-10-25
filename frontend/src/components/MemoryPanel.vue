<script setup>
import { reactive, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({ userId: Number, disabled: Boolean })
const labels = { brand_preference: '品牌偏好', usual_use: '常用用途', answer_style: '回答风格' }
const values = reactive({ brand_preference: '', usual_use: '', answer_style: '' })
const stored = ref({})
const error = ref('')
const notice = ref('')
const saving = ref(false)

async function refresh() {
  const owner = props.userId
  if (!owner) return
  try {
    const rows = await api.memories()
    if (props.userId !== owner) return
    const previous = stored.value
    stored.value = Object.fromEntries(rows.map(r => [r.key, r.value]))
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

watch(() => props.userId, () => {
  stored.value = {}
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
    <div v-for="(label, key) in labels" :key="key" class="memory-row">
      <label :for="`memory-${key}`">{{ label }}</label>
      <input :id="`memory-${key}`" v-model="values[key]" maxlength="120" :disabled="saving || disabled" :placeholder="`填写${label}`" />
      <button class="ghost" :disabled="saving || disabled || !values[key].trim() || values[key].trim() === stored[key]" @click="update(key)">保存</button>
      <button class="ghost" :disabled="saving || disabled || !stored[key]" @click="update(key, true)">删除</button>
    </div>
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
</style>
