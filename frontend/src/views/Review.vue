<script setup lang="ts">
// 代码审查页：发起表单（仓库/范围/依据）+ SSE 进度流 + findings 列表 + HitL apply/reject + 历史任务。
import { onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  applyFinding, getReviewTask, listReviewTasks, rejectFinding, streamReview,
} from "@/api/codeReview";
import { useChatStore } from "@/stores/chat";
import type { ReviewFinding, ReviewMode, ReviewTask, ReviewTaskDetail } from "@/api/types";

// name 供 BasicLayout 的 keep-alive include 匹配，实现路由切换后状态保留
defineOptions({ name: "ReviewPage" });

const chat = useChatStore();

// ── 发起表单 ──
const form = ref<{
  repo_id: string; mode: ReviewMode; base_ref: string; head_ref: string; review_basis: string;
}>({ repo_id: "", mode: "uncommitted", base_ref: "", head_ref: "", review_basis: "" });
const running = ref(false);
const logs = ref<string[]>([]);

// ── 结果与历史 ──
const result = ref<ReviewTaskDetail | null>(null);
const showHistory = ref(false);
const tasks = ref<ReviewTask[]>([]);

const SEV_META: Record<string, { type: "danger" | "warning" | "info"; label: string }> = {
  critical: { type: "danger", label: "critical" },
  warning: { type: "warning", label: "warning" },
  info: { type: "info", label: "info" },
};
const STATUS_META: Record<string, { type: "success" | "info" | "danger" | "warning"; label: string }> = {
  running: { type: "warning", label: "运行中" },
  success: { type: "success", label: "成功" },
  failed: { type: "danger", label: "失败" },
};

function progressText(d: Record<string, unknown>): string {
  switch (d.stage) {
    case "diff":
      return `变更获取完成：${d.files} 个文件 +${d.added} -${d.deleted}`
        + `（跳过 ${(d.skipped as unknown[] | undefined)?.length ?? 0} 个）`;
    case "rules":
      return `规则预检完成：${d.count} 条`;
    case "context":
      return `上下文装配完成：${d.count} 个文件`;
    case "review":
      return `审查 ${d.done}/${d.total}：${d.file}`;
    case "summarize":
      return "总体摘要已生成";
    default:
      return JSON.stringify(d);
  }
}

async function loadTasks() {
  tasks.value = await listReviewTasks().catch(() => [] as ReviewTask[]);
}

async function start() {
  if (!form.value.repo_id) {
    ElMessage.warning("请先选择仓库（右上角可登记新仓库）");
    return;
  }
  running.value = true;
  logs.value = [];
  result.value = null;
  await streamReview(
    { ...form.value },
    {
      onMeta: (d) => logs.value.push(`任务已创建：${d.task_id.slice(0, 8)}`),
      onProgress: (d) => logs.value.push(progressText(d)),
      onComment: (d) => logs.value.push(`评论 ${d.file}：${d.findings.length} 条`),
      onDone: async (d) => {
        running.value = false;
        // 以详情接口为准回填（含 severity 排序与 hitl 字段）
        result.value = await getReviewTask(d.task_id).catch(() => null);
        ElMessage.success(`审查完成：${d.findings.length} 条发现`);
        loadTasks();
      },
      onError: (d) => {
        running.value = false;
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

async function onApply(f: ReviewFinding) {
  try {
    await ElMessageBox.confirm(
      `将把该补丁应用到仓库并自动提交（git apply + commit）。\n${f.file} L${f.start_line}-L${f.end_line}`,
      "应用补丁",
      { type: "warning", confirmButtonText: "应用并提交", cancelButtonText: "取消" },
    );
  } catch {
    return; // 用户取消
  }
  try {
    const r = await applyFinding(f.id);
    f.hitl_status = "applied";
    f.applied_commit = r.applied_commit;
    ElMessage.success(`已应用并提交：${r.applied_commit.slice(0, 8)}`);
  } catch (e) {
    const err = e as { response?: { status: number; data?: { detail?: string } } };
    ElMessage.error(err.response?.data?.detail || "应用失败");
  }
}

async function onReject(f: ReviewFinding) {
  try {
    const r = await rejectFinding(f.id);
    f.hitl_status = r.hitl_status as ReviewFinding["hitl_status"];
    ElMessage.info("已拒绝该建议");
  } catch (e) {
    const err = e as { response?: { status: number; data?: { detail?: string } } };
    ElMessage.error(err.response?.data?.detail || "拒绝失败");
  }
}

async function openTask(t: ReviewTask) {
  showHistory.value = false;
  result.value = await getReviewTask(t.id).catch(() => {
    ElMessage.error("任务详情加载失败");
    return null;
  });
}

onMounted(() => {
  chat.loadRepos().then(() => {
    if (!form.value.repo_id && chat.repos.length) form.value.repo_id = chat.repos[0].id;
  }).catch(() => {});
  loadTasks();
});
</script>

<template>
  <div class="review">
    <div class="panel form-panel">
      <el-card shadow="never">
        <template #header>
          <div class="panel-head">
            <span>发起审查</span>
            <el-button link type="primary" @click="showHistory = true">历史任务</el-button>
          </div>
        </template>
        <el-form label-position="top">
          <el-form-item label="仓库">
            <el-select v-model="form.repo_id" placeholder="选择已登记仓库（不要求已索引）" style="width: 100%">
              <el-option v-for="r in chat.repos" :key="r.id" :label="r.name" :value="r.id">
                <span>{{ r.name }}</span>
                <el-tag size="small" effect="plain" style="margin-left: 8px">{{ r.index_status }}</el-tag>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item label="审查范围">
            <el-radio-group v-model="form.mode">
              <el-radio-button value="uncommitted">未提交变更</el-radio-button>
              <el-radio-button value="range">提交区间</el-radio-button>
              <el-radio-button value="commit">单个提交</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="form.mode === 'range'" label="base ref">
            <el-input v-model="form.base_ref" placeholder="起点分支/提交，如 main" />
          </el-form-item>
          <el-form-item v-if="form.mode !== 'uncommitted'" :label="form.mode === 'range' ? 'head ref' : 'commit'">
            <el-input v-model="form.head_ref" placeholder="终点分支/提交 hash" />
          </el-form-item>
          <el-form-item label="审查依据（可选）">
            <el-input
              v-model="form.review_basis"
              type="textarea"
              :rows="3"
              placeholder="业务规则文档 / 需求附注，模型将逐条对照检查"
            />
          </el-form-item>
          <el-button type="primary" :loading="running" style="width: 100%" @click="start">
            {{ running ? "审查中…" : "开始审查" }}
          </el-button>
        </el-form>
      </el-card>

      <el-card v-if="logs.length" shadow="never" class="log-card">
        <template #header>进度</template>
        <ul class="logs">
          <li v-for="(l, i) in logs" :key="i">{{ l }}</li>
        </ul>
      </el-card>
    </div>

    <div class="panel result-panel">
      <el-empty v-if="!result" description="尚无审查结果" />
      <template v-else>
        <el-card shadow="never" class="summary-card">
          <template #header>
            <div class="panel-head">
              <span>总体摘要</span>
              <el-tag :type="STATUS_META[result.status]?.type" size="small">
                {{ STATUS_META[result.status]?.label || result.status }}
              </el-tag>
            </div>
          </template>
          <p class="summary">{{ result.summary || result.error_message || "(无摘要)" }}</p>
          <p v-if="result.diff_stats?.files !== undefined" class="stats">
            {{ result.diff_stats.files }} 个文件变更，+{{ result.diff_stats.added }}
            -{{ result.diff_stats.deleted }}
            <template v-if="result.diff_stats.skipped?.length">
              ；跳过 {{ result.diff_stats.skipped.length }} 个（{{
                result.diff_stats.skipped.map((s) => s.reason).join("、") }}）
            </template>
            <span v-if="result.duration_ms != null">
              ；耗时 {{ (result.duration_ms / 1000).toFixed(1) }}s</span>
          </p>
        </el-card>

        <div v-if="!result.findings.length" class="no-finding">未发现问题 🎉</div>
        <el-card v-for="f in result.findings" :key="f.id" shadow="never" class="finding-card">
          <div class="finding-head">
            <el-tag :type="SEV_META[f.severity]?.type" size="small" effect="dark">{{ f.severity }}</el-tag>
            <el-tag size="small" effect="plain">{{ f.category }}</el-tag>
            <el-tag size="small" effect="plain" :type="f.source === 'rule' ? 'warning' : 'success'">
              {{ f.source === "rule" ? "规则" : "AI" }}
            </el-tag>
            <code class="loc">{{ f.file }} L{{ f.start_line }}-L{{ f.end_line }}</code>
            <span class="spacer" />
            <template v-if="f.hitl_status === 'pending'">
              <el-button
                size="small"
                type="primary"
                :disabled="!f.suggested_diff"
                @click="onApply(f)"
              >应用补丁</el-button>
              <el-button size="small" @click="onReject(f)">拒绝</el-button>
            </template>
            <el-tag v-else :type="f.hitl_status === 'applied' ? 'success' : 'info'" size="small">
              {{ f.hitl_status === "applied"
                ? `已应用 ${String(f.applied_commit).slice(0, 8)}` : "已拒绝" }}
            </el-tag>
          </div>
          <div class="finding-title">{{ f.title }}</div>
          <p class="finding-suggestion">{{ f.suggestion }}</p>
          <pre v-if="f.suggested_diff" class="diff">{{ f.suggested_diff }}</pre>
          <div v-else-if="f.hitl_status === 'pending'" class="no-diff">
            该发现无自动补丁（参考文字建议人工修复）
          </div>
        </el-card>
      </template>
    </div>

    <el-drawer v-model="showHistory" title="审查历史任务" size="460px">
      <el-table :data="tasks" size="small" class="history-table" @row-click="openTask">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ row.created_at.replace("T", " ").slice(0, 19) }}</template>
        </el-table-column>
        <el-table-column prop="mode" label="模式" width="100" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="STATUS_META[row.status]?.type" size="small">
              {{ STATUS_META[row.status]?.label || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文件数">
          <template #default="{ row }">{{ row.diff_stats?.files ?? 0 }}</template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<style scoped>
.review {
  display: grid;
  grid-template-columns: 360px 1fr;
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
.summary {
  margin: 0 0 8px;
  line-height: 1.7;
  white-space: pre-wrap;
}
.stats {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
}
.no-finding {
  margin-top: 24px;
  text-align: center;
  color: #6b7280;
}
.finding-card {
  margin-top: 12px;
}
.finding-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.loc {
  font-family: Consolas, Menlo, monospace;
  font-size: 12px;
  color: #374151;
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 4px;
}
.spacer {
  flex: 1;
}
.finding-title {
  margin-top: 10px;
  font-weight: 600;
}
.finding-suggestion {
  margin: 6px 0 0;
  color: #4b5563;
  line-height: 1.7;
  white-space: pre-wrap;
}
.diff {
  margin: 10px 0 0;
  padding: 10px 12px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 8px;
  font-family: Consolas, Menlo, monospace;
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
}
.no-diff {
  margin-top: 8px;
  color: #9ca3af;
  font-size: 12px;
}
.history-table {
  cursor: pointer;
}
</style>
