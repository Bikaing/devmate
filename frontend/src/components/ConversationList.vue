<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import { useChatStore } from "@/stores/chat";

const chat = useChatStore();
const route = useRoute();
const router = useRouter();

function open(id: string) {
  if (route.params.conversationId === id) return;
  router.push({ name: "chat-detail", params: { conversationId: id } });
}

// 软删除：确认后调 store 归档；删的是当前打开的会话则回聊天首页
async function remove(id: string, title: string) {
  try {
    await ElMessageBox.confirm(
      `删除会话「${title || "新会话"}」？删除后不再显示于列表。`,
      "删除会话",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return; // 用户取消
  }
  try {
    await chat.removeConversation(id);
  } catch (e) {
    ElMessage.error(`删除失败：${(e as Error).message || "请稍后重试"}`);
    return;
  }
  if (route.params.conversationId === id) router.push({ name: "chat" });
}
</script>

<template>
  <div class="conv-list">
    <div
      v-for="c in chat.conversations"
      :key="c.id"
      class="conv-item"
      :class="{ active: route.params.conversationId === c.id }"
      @click="open(c.id)"
    >
      <el-icon class="c-icon"><ChatLineRound /></el-icon>
      <span class="c-title">{{ c.title || "新会话" }}</span>
      <el-icon
        class="c-del"
        title="删除会话"
        @click.stop="remove(c.id, c.title)"
      ><Delete /></el-icon>
    </div>
    <div v-if="!chat.conversations.length" class="conv-empty">暂无会话</div>
  </div>
</template>

<style scoped>
.conv-list {
  flex: 1;
  overflow-y: auto;
  margin: 0 -4px;
}
.conv-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  color: rgba(255, 255, 255, 0.8);
  font-size: 13px;
  margin-bottom: 2px;
}
.conv-item:hover {
  background: rgba(255, 255, 255, 0.1);
}
.conv-item.active {
  background: rgba(255, 255, 255, 0.2);
  color: #fff;
  font-weight: 600;
}
.c-icon {
  flex-shrink: 0;
}
.c-del {
  margin-left: auto;
  flex-shrink: 0;
  visibility: hidden;
  color: rgba(255, 255, 255, 0.55);
}
.c-del:hover {
  color: #fff;
}
.conv-item:hover .c-del {
  visibility: visible;
}
.c-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-empty {
  padding: 14px;
  text-align: center;
  color: rgba(255, 255, 255, 0.45);
  font-size: 12px;
}
</style>
