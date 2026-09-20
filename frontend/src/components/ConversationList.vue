<script setup lang="ts">
import { useRoute, useRouter } from "vue-router";
import { useChatStore } from "@/stores/chat";

const chat = useChatStore();
const route = useRoute();
const router = useRouter();

function open(id: string) {
  if (route.params.conversationId === id) return;
  router.push({ name: "chat-detail", params: { conversationId: id } });
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
