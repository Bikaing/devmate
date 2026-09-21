<script setup lang="ts">
// 文档洞察页：左侧输入（标题+正文/文件读入）+ SSE 进度日志，右侧 Markdown 报告流式渲染 + 历史任务。
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { getDocTask, listDocTasks, streamDocInsight } from "@/api/docInsight";
import { renderMarkdown } from "@/utils/markdown";
import type { DocTask, DocTaskDetail } from "@/api/types";

// name 供 BasicLayout 的 keep-alive include 匹配，实现路由切换后状态保留
defineOptions({ name: "DocInsightPage" });

const CONTENT_MAX = 200_000; // 与后端 DOC_CONTENT_MAX 对齐，前端先拦一道

// ── 输入表单 ──
const form = ref({ title: "", content: "" });
const running = ref(false);
const logs = ref<string[]>([]);
const fileInput = ref<HTMLInputElement | null>(null);

// ── 报告与历史 ──
const liveReport = ref("");           // 流式期间的增量报告
const result = ref<DocTaskDetail | null>(null);
const showHistory = ref(false);
const tasks = ref<DocTask[]>([]);

const reportHtml = computed(() =>
  renderMarkdown(result.value?.report || liveReport.value),
);

const STATUS_META: Record<string, { type: "success" | "info" | "danger" | "warning"; label: string }> = {
  running: { type: "warning", label: "运行中" },
  success: { type: "success", label: "成功" },
  failed: { type: "danger", label: "失败" },
};

function progressText(d: Record<string, unknown>): string {
  switch (d.stage) {
    case "ingest":
      return `文档解析完成：切分 ${d.chunks} 个片段`;
    case "map":
      return `要点提炼完成：${d.noted}/${d.total} 个片段`;
    case "reduce_batch":
      return `要点归并中：第 ${d.batch}/${d.batches} 批`;
    case "report":
      return "报告生成中…";
    default:
      return JSON.stringify(d);
  }
}

function onPickFile() {
  fileInput.value?.click();
}

async function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = ""; // 允许重复选同一文件
  if (!file) return;
  if (!/\.(md|markdown|txt)$/i.test(file.name)) {
    ElMessage.warning("暂只支持 .md / .txt 文件（PDF 解析二期开放）");
    return;
  }
  const text = await file.text();
  if (text.length > CONTENT_MAX) {
    ElMessage.error(`文件过大（${text.length} 字符，上限 ${CONTENT_MAX}），请拆分后分批洞察`);
    return;
  }
  if (!form.value.title) form.value.title = file.name.replace(/\.(md|markdown|txt)$/i, "");
  form.value.content = text;
  ElMessage.success(`已读入 ${file.name}（${text.length} 字符）`);
}

async function loadTasks() {
  tasks.value = await listDocTasks().catch(() => [] as DocTask[]);
}

async function start() {
  if (!form.value.title.trim()) {
    ElMessage.warning("请填写文档标题");
    return;
  }
  if (!form.value.content.trim()) {
    ElMessage.warning("请填写文档内容或读入 .md/.txt 文件");
    return;
  }
  if (form.value.content.length > CONTENT_MAX) {
    ElMessage.error(`文档过长（${form.value.content.length} 字符，上限 ${CONTENT_MAX}）`);
    return;
  }
  running.value = true;
  logs.value = [];
  liveReport.value = "";
  result.value = null;
  await streamDocInsight(
    { title: form.value.title.trim(), content: form.value.content },
    {
      onMeta: (d) => logs.value.push(`任务已创建：${d.task_id.slice(0, 8)}`),
      onProgress: (d) => logs.value.push(progressText(d)),
      onToken: (d) => { liveReport.value += d.text; },
      onDone: async (d) => {
        running.value = false;
        // 以详情接口为准回填（含 status/created_at），失败则用流内 report 兜底
        result.value = await getDocTask(d.task_id).catch(() => ({
          id: d.task_id, title: form.value.title, content: form.value.content,
          status: "success" as const, report: d.report, error_message: null,
          duration_ms: d.duration_ms, created_at: new Date().toISOString(),
        }));
        liveReport.value = "";
        ElMessage.success("洞察报告已生成");
        loadTasks();
      },
      onError: (d) => {
        running.value = false;
        liveReport.value = "";
        logs.value.push(`失败：${d.message}`);
        ElMessage.error(d.message);
        loadTasks();
      },
      onHttpError: (s, detail) => {
        running.value = false;
        ElMessage.error(`${detail || "请求失败"}（${s}）`);
      },
    },
  );
  running.value = false; // 流无 done/error 结束（断网等）的兜底
}

async function openTask(t: DocTask) {
  showHistory.value = false;
  liveReport.value = "";
  result.value = await getDocTask(t.id).catch(() => {
    ElMessage.error("任务详情加载失败");
    return null;
  });
}

onMounted(loadTasks);
</script>

<template>
  <div class="doc-insight">
    <div class="panel form-panel">
      <el-card shadow="never">
        <template #header>
          <div class="panel-head">
            <span>文档洞察</span>
            <el-button link type="primary" @click="showHistory = true">历史任务</el-button>
          </div>
        </template>
        <el-form label-position="top">
          <el-form-item label="文档标题">
            <el-input v-model="form.title" maxlength="200" placeholder="如：XX 项目技术方案" :disabled="running" />
          </el-form-item>
          <el-form-item>
            <template #label>
              <div class="content-label">
                <span>文档内容（md / txt）</span>
                <el-button link type="primary" :disabled="running" @click="onPickFile">读入文件</el-button>
              </div>
            </template>
            <el-input
              v-model="form.content"
              type="textarea"
              :rows="12"
              resize="none"
              placeholder="粘贴文档正文，或点「读入文件」选择本地 .md/.txt"
              :disabled="running"
            />
            <div class="content-count" :class="{ over: form.content.length > CONTENT_MAX }">
              {{ form.content.length }} / {{ CONTENT_MAX }} 字符
            </div>
          </el-form-item>
          <el-button type="primary" :loading="running" style="width: 100%" @click="start">
            {{ running ? "洞察中…" : "生成洞察报告" }}
          </el-button>
          <input
            ref="fileInput"
            type="file"
            accept=".md,.markdown,.txt"
            style="display: none"
            @change="onFileChange"
          />
        </el-form>
      </el-card>

      <el-card v-if="logs.length" shadow="never" class="log-card">
        <template #header>进度</template>
        <ul class="logs">
          <li v-for="(l, i) in logs" :key="i">{{ l }}</li>
        </ul>
      </el-card>
    </div>

    <div class="panel report-panel">
      <el-empty v-if="!reportHtml" description="尚无洞察报告" />
      <el-card v-else shadow="never" class="report-card">
        <template #header>
          <div class="panel-head">
            <span>洞察报告{{ result ? `：${result.title}` : "" }}</span>
            <span class="head-right">
              <el-tag v-if="running" type="warning" size="small">生成中</el-tag>
              <el-tag v-else-if="result" :type="STATUS_META[result.status]?.type" size="small">
                {{ STATUS_META[result.status]?.label || result.status }}
              </el-tag>
              <span v-if="result?.duration_ms != null" class="duration">
                耗时 {{ (result.duration_ms / 1000).toFixed(1) }}s
              </span>
            </span>
          </div>
        </template>
        <div class="md-body" v-html="reportHtml"></div>
        <div v-if="result?.status === 'failed'" class="failed-note">
          {{ result.error_message }}
        </div>
      </el-card>
    </div>

    <el-drawer v-model="showHistory" title="洞察历史任务" size="460px">
      <el-table :data="tasks" size="small" class="history-table" @row-click="openTask">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ row.created_at.replace("T", " ").slice(0, 19) }}</template>
        </el-table-column>
        <el-table-column prop="title" label="标题" show-overflow-tooltip />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="STATUS_META[row.status]?.type" size="small">
              {{ STATUS_META[row.status]?.label || row.status }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<style scoped>
.doc-insight {
  display: grid;
  grid-template-columns: 400px 1fr;
  gap: 16px;
  height: 100%;
  padding: 20px 24px;
  overflow: hidden;
}
.panel {
  overflow-y: auto;
  min-height: 0;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  gap: 8px;
}
.head-right {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 400;
}
.duration {
  color: #6b7280;
  font-size: 12px;
}
.content-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}
.content-count {
  width: 100%;
  text-align: right;
  font-size: 12px;
  color: #9ca3af;
  line-height: 1.8;
}
.content-count.over {
  color: #dc2626;
}
.log-card {
  margin-top: 12px;
}
.logs {
  margin: 0;
  padding-left: 18px;
  font-family: Consolas, Menlo, monospace;
  font-size: 12px;
  color: #4b5563;
  line-height: 1.9;
}
.failed-note {
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 8px;
  background: #fef2f2;
  color: #b91c1c;
  font-size: 13px;
}
.history-table {
  cursor: pointer;
}
</style>
