<script setup lang="ts">
import type { Citation } from "@/api/types";

defineProps<{ citations: Citation[] }>();
</script>

<template>
  <div v-if="citations && citations.length" class="citations">
    <div class="cite-head">
      <el-icon><Document /></el-icon>
      <span>引用来源 · {{ citations.length }}</span>
    </div>
    <div class="cite-list">
      <div v-for="(c, i) in citations" :key="i" class="cite-item">
        <el-icon class="file-ico"><Tickets /></el-icon>
        <span class="file" :title="c.file">{{ c.file }}</span>
        <span class="lines">L{{ c.start_line }}-L{{ c.end_line }}</span>
        <el-tag v-if="c.symbol" size="small" type="info" effect="plain">{{ c.symbol }}</el-tag>
      </div>
    </div>
  </div>
</template>

<style scoped>
.citations {
  margin-top: 12px;
  border-top: 1px dashed #e5e7eb;
  padding-top: 10px;
}
.cite-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 8px;
}
.cite-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cite-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  background: #f8fafc;
  border: 1px solid #eef2f7;
  border-radius: 6px;
  padding: 6px 10px;
}
.file-ico {
  color: #1d4ed8;
  flex-shrink: 0;
}
.file {
  font-family: Consolas, monospace;
  color: #374151;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.lines {
  color: #1d4ed8;
  font-family: Consolas, monospace;
  flex-shrink: 0;
}
</style>
