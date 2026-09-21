<script setup lang="ts">
import { computed } from "vue";
import { renderMarkdown } from "@/utils/markdown";
import CitationCard from "./CitationCard.vue";
import type { UiMessage } from "@/api/types";

const props = defineProps<{ message: UiMessage }>();

const isUser = computed(() => props.message.role === "user");
const html = computed(() => renderMarkdown(props.message.content));
const pending = computed(() => props.message.streaming && !props.message.content);
</script>

<template>
  <div class="msg-row" :class="isUser ? 'right' : 'left'">
    <div class="avatar" :class="{ user: isUser }">
      <el-icon v-if="isUser"><UserFilled /></el-icon>
      <el-icon v-else><Cpu /></el-icon>
    </div>

    <div class="bubble" :class="{ user: isUser, error: message.msgType === 'error' }">
      <el-alert
        v-if="message.msgType === 'error'"
        type="error"
        :closable="false"
        show-icon
        title="回答失败"
        :description="message.content"
      />
      <template v-else>
        <div v-if="isUser" class="plain">{{ message.content }}</div>
        <div v-else-if="pending" class="thinking">
          <span class="dot"></span><span class="dot"></span><span class="dot"></span>
        </div>
        <div v-else class="md-body" v-html="html"></div>
        <span v-if="!isUser && message.streaming && message.content" class="cursor">▋</span>
      </template>

      <CitationCard
        v-if="!isUser && message.citations && message.citations.length"
        :citations="message.citations"
      />

      <div
        v-if="!isUser && message.webSources && message.webSources.length"
        class="web-sources"
      >
        <div class="ws-title">网页参考</div>
        <a
          v-for="(w, i) in message.webSources"
          :key="i"
          :href="w.href"
          target="_blank"
          rel="noopener"
          class="ws-item"
        >{{ i + 1 }}. {{ w.title || w.href }}</a>
      </div>
    </div>
  </div>
</template>

<style scoped>
.msg-row {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
}
.msg-row.right {
  flex-direction: row-reverse;
}
.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #1d4ed8;
  color: #fff;
  font-size: 18px;
}
.avatar.user {
  background: #64748b;
}
.bubble {
  max-width: 78%;
  background: #fff;
  border: 1px solid #eef2f7;
  border-radius: 12px;
  padding: 12px 16px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.bubble.user {
  background: #1d4ed8;
  color: #fff;
  border-color: #1d4ed8;
}
.bubble.error {
  background: #fff;
  border-color: #fecaca;
}
.plain {
  white-space: pre-wrap;
  word-break: break-word;
}
.cursor {
  animation: blink 1s step-start infinite;
  color: #1d4ed8;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.thinking {
  display: flex;
  gap: 4px;
  padding: 4px 0;
}
.web-sources {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #e5e7eb;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ws-title {
  font-size: 12px;
  color: #6b7280;
}
.ws-item {
  font-size: 12px;
  color: #1d4ed8;
  text-decoration: none;
  word-break: break-all;
}
.ws-item:hover {
  text-decoration: underline;
}
.thinking .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #9ca3af;
  animation: bounce 1.2s infinite ease-in-out;
}
.thinking .dot:nth-child(2) {
  animation-delay: 0.2s;
}
.thinking .dot:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes bounce {
  0%,
  80%,
  100% {
    transform: scale(0.6);
    opacity: 0.5;
  }
  40% {
    transform: scale(1);
    opacity: 1;
  }
}
</style>
