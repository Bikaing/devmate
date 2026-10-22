<script setup lang="ts">
import { computed } from 'vue';
import { formatReviewText } from '@/utils/reviewText';
const props = defineProps<{ text: string }>();
const content = computed(() => formatReviewText(props.text));
</script>

<template><div class="review-text" v-html="content" /></template>

<style scoped>
.review-text { font-size: 14px; line-height: 1.85; color: #475569; overflow-wrap: anywhere; }
.review-text :deep(p) { margin: 0 0 12px; }
.review-text :deep(p:last-child) { margin-bottom: 0; }
.review-text :deep(strong) { color: #1e293b; font-weight: 600; }
.review-text :deep(ul), .review-text :deep(ol) { margin: 8px 0 16px; padding-left: 24px; }
.review-text :deep(li) { padding-left: 4px; margin: 6px 0; }
.review-text :deep(li p) { margin: 0; }
.review-text :deep(code) { padding: 2px 5px; border-radius: 4px; background: #f1f5f9; color: #334155; font: 0.9em/1.6 Consolas, Menlo, monospace; }
.review-text :deep(pre) { padding: 12px; overflow-x: auto; background: #f8fafc; border-radius: 6px; }
.review-text :deep(pre code) { padding: 0; background: none; }
.review-text :deep(h1), .review-text :deep(h2), .review-text :deep(h3) { font-size: 15px; margin: 16px 0 8px; color: #1e293b; }
.review-text :deep(blockquote) { margin: 12px 0; padding-left: 12px; border-left: 3px solid #cbd5e1; }
.review-text :deep(table) { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; }
.review-text :deep(th), .review-text :deep(td) { padding: 6px 10px; border: 1px solid #e2e8f0; }
</style>
