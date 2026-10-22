<script setup lang="ts">
import { computed, onBeforeUnmount, onDeactivated, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { applyFix, cancelFix, getFix, listFixes, startFix } from '@/api/codeFix';
import type { FixTask, VerificationProfile } from '@/api/codeFix';

const props = defineProps<{ findingId: string; pending: boolean }>();
const emit = defineEmits<{ applied: [] }>();
const visible = ref(false);
const loading = ref(false);
const trusted = ref(false);
const profile = ref<VerificationProfile>('pytest');
const history = ref<FixTask[]>([]);
const task = ref<FixTask | null>(null);
let timer: ReturnType<typeof setTimeout> | undefined;
let generation = 0;
const labels: Record<string, string> = {
  pending: '等待执行', preparing: '准备工作区 / 基线验证', analyzing: '历史分析阶段', fixing: '生成修复',
  validating: '验证中', ready: '待确认应用', failed: '修复失败', cancelled: '已取消',
  applied: '已应用到工作树', passed: '通过', insufficient: '验证不足',
  error: '执行异常', not_run: '未执行', completed: '本轮结束',
};
const active = computed(() => !!task.value && ['pending', 'preparing', 'analyzing', 'fixing', 'validating'].includes(task.value.status));
const occupied = computed(() => active.value || task.value?.status === 'ready');
function stopPolling() {
  generation++;
  if (timer) clearTimeout(timer);
}
function message(error: unknown) {
  const e = error as { response?: { data?: { detail?: string } } };
  ElMessage.error(e.response?.data?.detail || '修复请求失败，可重新打开查看任务');
}
async function selectTask(id: string) {
  stopPolling();
  const current = generation;
  const refresh = async () => {
    try {
      const data = await getFix(id);
      if (current !== generation || !visible.value) return;
      task.value = data;
      if (data.status === 'applied') emit('applied');
      if (active.value) timer = setTimeout(refresh, 1500);
    } catch (error) {
      if (current === generation) message(error);
    }
  };
  await refresh();
}
async function open() {
  visible.value = true;
  loading.value = true;
  try {
    history.value = await listFixes(props.findingId);
    if (history.value.length) await selectTask(history.value[0].id);
  } catch (error) { message(error); }
  finally { loading.value = false; }
}
async function start() {
  loading.value = true;
  try {
    const created = await startFix(props.findingId, profile.value);
    history.value = await listFixes(props.findingId);
    await selectTask(created.id);
  } catch (error) { message(error); }
  finally { loading.value = false; }
}
async function cancel() {
  if (!task.value) return;
  loading.value = true;
  try {
    await cancelFix(task.value.id);
    await selectTask(task.value.id);
  } catch (error) { message(error); }
  finally { loading.value = false; }
}
async function apply() {
  if (!task.value) return;
  try {
    await ElMessageBox.confirm('将把展示的修复应用到原仓库工作树，不自动提交。若原仓库已变化将拒绝应用。检查通过仅代表下列验证通过，请审阅 diff 后确认。',
      '确认应用修复', { confirmButtonText: '确认应用', cancelButtonText: '取消', type: 'warning' });
  } catch { return; }
  loading.value = true;
  try {
    await applyFix(task.value.id);
    await selectTask(task.value.id);
    emit('applied');
    ElMessage.success('修复已应用，请自行审阅并提交');
  } catch (error) { message(error); }
  finally { loading.value = false; }
}
onBeforeUnmount(stopPolling);
onDeactivated(() => { visible.value = false; stopPolling(); });
</script>

<template>
  <el-button size="small" type="primary" plain @click="open">修复与验证</el-button>
  <el-drawer v-model="visible" title="修复与验证" size="70%" @closed="stopPolling">
    <p>在独立工作区修复所选问题对应的单个文件，再运行验证。失败证据反馈给修复 Agent，最多重试两轮。通过验证后可确认应用。</p>
    <el-alert type="info" :closable="false" title="验证会执行仓库中的测试或构建代码，请仅对可信的本地仓库使用。忽略文件、依赖目录不会复制，也不会自动安装依赖。" />
    <div class="controls">
      <el-select v-model="profile" :disabled="occupied" style="width: 260px">
        <el-option label="Python · pytest（仓库根目录）" value="pytest" />
        <el-option label="Python · unittest（tests 目录）" value="unittest" />
        <el-option label="Node · npm run build（根目录）" value="npm-build" />
      </el-select>
      <el-checkbox v-model="trusted">我信任此仓库并允许执行所选检查</el-checkbox>
      <el-button type="primary" :loading="loading" :disabled="!pending || !trusted || occupied" @click="start">尝试修复</el-button>
    </div>
    <el-select v-if="history.length" placeholder="修复历史" style="width: 100%; margin-bottom: 16px" @change="selectTask">
      <el-option v-for="item in history" :key="item.id" :value="item.id"
        :label="`${item.created_at.slice(0, 19)} · ${labels[item.status] || item.status} · ${item.id.slice(0, 8)}`" />
    </el-select>
    <template v-if="task">
      <div class="controls">
        <el-tag>{{ labels[task.status] || task.status }}</el-tag>
        <span>第 {{ task.attempt_count }} / {{ task.max_attempts }} 轮</span>
        <span>验证：{{ labels[task.verification_status] || task.verification_status }}</span>
        <el-button v-if="occupied" :disabled="loading" @click="cancel">{{ task.status === 'ready' ? '放弃此修复' : '取消任务' }}</el-button>
        <el-button v-if="task.status === 'ready' && task.verification_status === 'passed'" type="primary" :loading="loading" :disabled="!pending" @click="apply">确认应用</el-button>
        <el-button :disabled="loading" @click="selectTask(task.id)">刷新</el-button>
      </div>
      <el-alert v-if="task.error_message" :title="task.error_message" type="error" :closable="false" />
      <el-alert v-if="task.cleanup_error" title="工作区自动清理失败，需管理员检查后台记录" type="warning" :closable="false" />
      <h3>最终 diff</h3>
      <pre class="evidence">{{ task.patch || '尚无补丁' }}</pre>
      <h3>修复前基线</h3>
      <div v-for="(check, i) in task.baseline_results" :key="i">
        <p>{{ check.argv.join(' ') }} · {{ check.status === 'failed' ? '基线验证未通过' : (labels[check.status] || check.status) }} · 退出码 {{ check.exit_code ?? '—' }}</p>
        <pre class="evidence">{{ check.error || '' }}{{ '\n' + check.output }}</pre>
      </div>
      <h3>各轮验证证据</h3>
      <el-collapse>
        <el-collapse-item v-for="attempt in task.attempts" :key="attempt.attempt_number"
          :title="`第 ${attempt.attempt_number} 轮 · ${labels[attempt.verification_status] || attempt.verification_status}`"
          :name="attempt.attempt_number">
          <p v-if="attempt.error_message">{{ attempt.error_message }}</p>
          <div v-for="(check, i) in attempt.verification_results" :key="i">
            <p>{{ check.argv.join(' ') }} · 退出码 {{ check.exit_code ?? '—' }} · {{ check.duration_ms }}ms</p>
            <pre class="evidence">{{ check.error || '' }}{{ '\n' + check.output }}</pre>
          </div>
          <pre v-if="attempt.patch" class="evidence">{{ attempt.patch }}</pre>
        </el-collapse-item>
      </el-collapse>
    </template>
  </el-drawer>
</template>

<style scoped>
.controls { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 16px 0; }
.evidence { background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px; overflow: auto; max-height: 480px; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.6 Consolas, monospace; }
</style>
