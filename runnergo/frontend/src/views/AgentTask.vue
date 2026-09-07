<template>
  <section class="agent-task">
    <h2>智能测试引擎</h2>
    <textarea v-model="description" placeholder="描述要验证的需求" :disabled="running" />
    <button :disabled="running || !description.trim()" @click="start">{{ running ? '执行中…' : '生成并执行' }}</button>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="status" class="status">
      <p>流程：{{ status.steps.join(' → ') }}</p>
      <p>用例：{{ status.test_cases.length }}，通过：{{ passed }}，失败：{{ failed }}</p>
      <p>覆盖率：{{ coverage }}%</p>
      <pre>{{ status.quality.summary || '评估完成' }}</pre>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { getAgentStatus, runAgent, type AgentStatus } from '../api/agent'

const description = ref('')
const status = ref<AgentStatus | null>(null)
const error = ref('')
const running = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
const passed = computed(() => status.value?.results.filter((item) => item.status === 'passed').length || 0)
const failed = computed(() => status.value?.results.filter((item) => item.status === 'failed' || item.status === 'error').length || 0)
const coverage = computed(() => Math.round(Number(status.value?.quality.coverage_rate || 0) * 100))

async function start() {
  running.value = true; error.value = ''
  try {
    status.value = await runAgent({ description: description.value })
    if (status.value.task.id) {
      timer = setInterval(async () => {
        try { status.value = await getAgentStatus(status.value!.task.id!) } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
      }, 1500)
    }
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) } finally { running.value = false }
}
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.agent-task { max-width: 720px; padding: 24px; }
textarea { display: block; width: 100%; min-height: 120px; margin: 12px 0; }
button { padding: 8px 18px; }
.error { color: #d33; }
.status { margin-top: 20px; padding: 16px; background: #f6f8fa; }
</style>
