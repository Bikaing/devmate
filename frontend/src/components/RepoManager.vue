<script setup lang="ts">
import { onUnmounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { registerRepo, triggerIndex } from "@/api/codeQa";
import { useChatStore } from "@/stores/chat";
import type { IndexStatus, Repo } from "@/api/types";

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{ (e: "update:modelValue", v: boolean): void; (e: "changed"): void }>();

const chat = useChatStore();
const form = reactive({ path: "", name: "" });
const registering = ref(false);

const STATUS_META: Record<IndexStatus, { label: string; type: "success" | "warning" | "info" | "danger" }> = {
  indexed: { label: "已索引", type: "success" },
  indexing: { label: "索引中", type: "warning" },
  pending: { label: "待索引", type: "info" },
  failed: { label: "索引失败", type: "danger" },
};

// 有仓库在索引中时轮询刷新状态，全部落定后停止
let timer: number | null = null;
function stopPolling() {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}
function startPolling() {
  stopPolling();
  timer = window.setInterval(async () => {
    await chat.loadRepos().catch(() => {});
    emit("changed");
    if (!chat.repos.some((r) => r.index_status === "indexing")) stopPolling();
  }, 3000);
}
onUnmounted(stopPolling);

watch(
  () => props.modelValue,
  async (visible) => {
    if (visible) {
      await chat.loadRepos().catch(() => {});
      emit("changed");
      if (chat.repos.some((r) => r.index_status === "indexing")) startPolling();
    } else {
      stopPolling();
    }
  },
);

function close() {
  emit("update:modelValue", false);
}

async function onRegister() {
  const path = form.path.trim();
  if (!path) {
    ElMessage.warning("请填写仓库本地路径");
    return;
  }
  registering.value = true;
  try {
    await registerRepo(path, form.name.trim());
    ElMessage.success("登记成功，请点击「索引」建立索引");
    form.path = "";
    form.name = "";
    await chat.loadRepos();
    emit("changed");
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } };
    ElMessage.error(err.response?.data?.detail || "登记失败");
  } finally {
    registering.value = false;
  }
}

async function onIndex(r: Repo) {
  try {
    await triggerIndex(r.id);
    ElMessage.info(`已开始索引「${r.name}」，完成后状态变为已索引`);
    await chat.loadRepos();
    emit("changed");
    startPolling();
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } };
    ElMessage.error(err.response?.data?.detail || "触发索引失败");
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="仓库管理"
    width="640px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="repo-list">
      <div v-if="!chat.repos.length" class="none">还没有登记的仓库，先在下方登记一个本地路径。</div>
      <div v-for="r in chat.repos" :key="r.id" class="repo-row">
        <div class="repo-main">
          <div class="repo-name">
            {{ r.name }}
            <el-tag size="small" :type="STATUS_META[r.index_status].type" effect="light">
              {{ STATUS_META[r.index_status].label }}
            </el-tag>
          </div>
          <div class="repo-path" :title="r.path">{{ r.path }}</div>
        </div>
        <el-button
          type="primary"
          plain
          size="small"
          :loading="r.index_status === 'indexing'"
          :disabled="r.index_status === 'indexing'"
          @click="onIndex(r)"
        >
          {{ r.index_status === "indexed" ? "重新索引" : "索引" }}
        </el-button>
      </div>
    </div>

    <el-divider />

    <div class="reg-form">
      <div class="reg-title">登记新仓库</div>
      <el-input
        v-model="form.path"
        placeholder="本地仓库绝对路径，如 D:\Project"
        clearable
      />
      <el-input v-model="form.name" placeholder="展示名（可空，默认取路径末段）" clearable />
      <el-button type="primary" :loading="registering" @click="onRegister">登记</el-button>
    </div>

    <template #footer>
      <el-button @click="close">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.repo-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 260px;
  overflow-y: auto;
}
.none {
  color: #9ca3af;
  font-size: 13px;
  padding: 8px 0;
}
.repo-row {
  display: flex;
  align-items: center;
  gap: 12px;
  border: 1px solid #eef2f7;
  border-radius: 8px;
  padding: 10px 12px;
}
.repo-main {
  flex: 1;
  min-width: 0;
}
.repo-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
}
.repo-path {
  font-size: 12px;
  color: #9ca3af;
  font-family: Consolas, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 2px;
}
.reg-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.reg-title {
  font-size: 13px;
  color: #6b7280;
  font-weight: 600;
}
.reg-form .el-button {
  align-self: flex-start;
}
</style>
