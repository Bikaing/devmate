<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Setting } from "@element-plus/icons-vue";
import ChatMessage from "@/components/ChatMessage.vue";
import RepoManager from "@/components/RepoManager.vue";
import { streamChat } from "@/api/sse";
import { listMessages } from "@/api/codeQa";
import { useChatStore } from "@/stores/chat";
import type { UiMessage } from "@/api/types";

const route = useRoute();
const router = useRouter();
const chat = useChatStore();

const messages = ref<UiMessage[]>([]);
const input = ref("");
const sending = ref(false);
const conversationId = ref<string | null>(null);
const scroller = ref<HTMLElement | null>(null);
const showRepoManager = ref(false);

const activeRepoId = computed({
  get: () => chat.activeRepoId,
  set: (v: string) => chat.setActiveRepo(v),
});
const repoReady = computed(() => chat.activeRepo?.index_status === "indexed");
const canSend = computed(
  () => !!activeRepoId.value && repoReady.value && !!input.value.trim() && !sending.value,
);

async function scrollToBottom() {
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}

async function loadHistory(id: string) {
  const hist = await listMessages(id);
  messages.value = hist.map((m) => ({
    id: m.id,
    role: m.role === "user" ? "user" : "assistant",
    msgType: m.msg_type === "error" ? "error" : "text",
    content: m.content,
    citations: m.citations || undefined,
  }));
  scrollToBottom();
}

onMounted(async () => {
  await chat.loadRepos().catch(() => {});
  const cid = route.params.conversationId as string | undefined;
  if (cid) {
    conversationId.value = cid;
    await loadHistory(cid).catch(() => ElMessage.error("加载历史消息失败"));
  }
});

// 侧边栏切换会话 / 新建会话时同步消息区
watch(
  () => route.params.conversationId,
  async (cid) => {
    if (sending.value) return;
    if (cid && cid !== conversationId.value) {
      conversationId.value = cid as string;
      await loadHistory(cid as string).catch(() => {});
    } else if (!cid) {
      conversationId.value = null;
      messages.value = [];
    }
  },
);

function onKeydown(e: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
}

// 仓库列表变化后纠正当前选中（被删/不可用时回落到第一个可用仓库）
function onRepoChanged() {
  if (chat.activeRepoId && !chat.repos.some((r) => r.id === chat.activeRepoId)) {
    chat.setActiveRepo("");
  }
  if (!chat.activeRepoId) {
    const preferred = chat.indexedRepos[0] || chat.repos[0];
    if (preferred) chat.setActiveRepo(preferred.id);
  }
}

async function send() {
  if (!canSend.value) return;
  const query = input.value.trim();
  input.value = "";
  messages.value.push({ role: "user", msgType: "text", content: query });
  messages.value.push({ role: "assistant", msgType: "text", content: "", streaming: true });
  const assistant = messages.value[messages.value.length - 1];
  sending.value = true;
  scrollToBottom();

  await streamChat(
    { repo_id: activeRepoId.value, query, conversation_id: conversationId.value },
    {
      onMeta: (d) => {
        // 首轮：拿到新会话 id，替换 URL（不触发历史重载）并刷新侧边栏
        if (!conversationId.value) {
          conversationId.value = d.conversation_id;
          router.replace({
            name: "chat-detail",
            params: { conversationId: d.conversation_id },
          });
          chat.loadConversations().catch(() => {});
        }
      },
      onToken: (d) => {
        assistant.content += d.text;
        scrollToBottom();
      },
      onDone: (d) => {
        assistant.streaming = false;
        assistant.id = d.message_id;
        assistant.citations = d.citations || [];
        scrollToBottom();
      },
      onError: (d) => {
        assistant.streaming = false;
        assistant.msgType = "error";
        assistant.content = d.message;
        scrollToBottom();
      },
      onHttpError: (status, detail) => {
        assistant.streaming = false;
        assistant.msgType = "error";
        assistant.content = detail || `请求失败（HTTP ${status}）`;
        if (status === 409) ElMessage.warning(detail);
        scrollToBottom();
      },
    },
  );
  sending.value = false;
}
</script>

<template>
  <div class="chat">
    <div class="app-header">
      <div class="title">代码问答</div>
      <div class="repo-picker">
        <span class="label">仓库</span>
        <el-select
          v-model="activeRepoId"
          placeholder="选择仓库"
          size="default"
          style="width: 240px"
          :disabled="sending"
        >
          <el-option
            v-for="r in chat.repos"
            :key="r.id"
            :label="r.name"
            :value="r.id"
            :disabled="r.index_status !== 'indexed'"
          >
            <span>{{ r.name }}</span>
            <el-tag
              size="small"
              :type="r.index_status === 'indexed' ? 'success' : 'info'"
              effect="plain"
              style="margin-left: 8px"
            >
              {{ r.index_status }}
            </el-tag>
          </el-option>
        </el-select>
        <el-button :icon="Setting" @click="showRepoManager = true">管理仓库</el-button>
      </div>
    </div>

    <div ref="scroller" class="msg-scroller">
      <div v-if="!chat.repos.length" class="empty">
        <el-empty description="还没有登记的仓库">
          <el-button type="primary" @click="showRepoManager = true">登记并索引仓库</el-button>
        </el-empty>
      </div>
      <div v-else-if="!messages.length" class="empty">
        <el-empty description="输入问题开始向仓库提问" />
      </div>
      <div v-else class="msg-list">
        <ChatMessage v-for="(m, i) in messages" :key="i" :message="m" />
      </div>
    </div>

    <div class="composer">
      <el-alert
        v-if="activeRepoId && !repoReady"
        type="warning"
        :closable="false"
        show-icon
        title="当前仓库索引未就绪，无法问答，请切换到已索引（indexed）的仓库"
        style="margin-bottom: 10px"
      />
      <div class="input-row">
        <el-input
          v-model="input"
          type="textarea"
          :rows="2"
          resize="none"
          placeholder="向仓库提问…（Enter 发送，Shift+Enter 换行）"
          :disabled="sending"
          @keydown="onKeydown"
        />
        <el-button
          type="primary"
          :loading="sending"
          :disabled="!canSend"
          class="send-btn"
          @click="send"
        >
          发送
        </el-button>
      </div>
    </div>

    <RepoManager v-model="showRepoManager" @changed="onRepoChanged" />
  </div>
</template>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.repo-picker {
  display: flex;
  align-items: center;
  gap: 10px;
}
.repo-picker .label {
  font-size: 13px;
  color: #6b7280;
}
.msg-scroller {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px 32px;
}
.msg-list {
  max-width: 900px;
  margin: 0 auto;
}
.empty {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.composer {
  border-top: 1px solid #e5e7eb;
  background: #fff;
  padding: 14px 32px 18px;
}
.input-row {
  max-width: 900px;
  margin: 0 auto;
  display: flex;
  gap: 12px;
  align-items: flex-end;
}
.input-row :deep(.el-textarea) {
  flex: 1;
}
.send-btn {
  height: 54px;
  width: 88px;
}
</style>
