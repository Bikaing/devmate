<script setup lang="ts">
import { onMounted } from "vue";
import { useRouter } from "vue-router";
import { useChatStore } from "@/stores/chat";

const router = useRouter();
const chat = useChatStore();

onMounted(() => {
  chat.loadRepos().catch(() => {});
});

interface AgentCard {
  key: string;
  title: string;
  desc: string;
  icon: string;
  color: string;
  enabled: boolean;
}

const agents: AgentCard[] = [
  {
    key: "code_qa",
    title: "代码问答",
    desc: "基于仓库索引的语义检索问答，回答附带文件行号与符号引用",
    icon: "ChatDotRound",
    color: "#1d4ed8",
    enabled: true,
  },
  {
    key: "code_review",
    title: "代码审查",
    desc: "Git 变更审查：风险定位 + 文字建议 + 一键补丁修复",
    icon: "View",
    color: "#0891b2",
    enabled: true,
  },
  {
    key: "doc_insight",
    title: "文档洞察",
    desc: "文档理解与知识抽取（规划中）",
    icon: "Document",
    color: "#7c3aed",
    enabled: false,
  },
  {
    key: "incident_triage",
    title: "事件分诊",
    desc: "告警定位与根因分析（规划中）",
    icon: "Warning",
    color: "#dc2626",
    enabled: false,
  },
];

function open(a: AgentCard) {
  if (!a.enabled) return;
  router.push({ name: a.key === "code_review" ? "review" : "chat" });
}
</script>

<template>
  <div class="home">
    <div class="hero">
      <h2>你好，欢迎使用 DevMate 👋</h2>
      <p>选择一个 Agent 开始工作。当前已索引仓库 {{ chat.indexedRepos.length }} 个。</p>
    </div>

    <div class="card-grid">
      <div
        v-for="a in agents"
        :key="a.key"
        class="agent-card"
        :class="{ disabled: !a.enabled }"
        @click="open(a)"
      >
        <div class="card-icon" :style="{ background: a.color }">
          <el-icon :size="26"><component :is="a.icon" /></el-icon>
        </div>
        <div class="card-body">
          <div class="card-title">
            {{ a.title }}
            <el-tag v-if="!a.enabled" size="small" type="info" effect="plain">敬请期待</el-tag>
          </div>
          <div class="card-desc">{{ a.desc }}</div>
        </div>
        <el-icon v-if="a.enabled" class="arrow"><ArrowRight /></el-icon>
      </div>
    </div>
  </div>
</template>

<style scoped>
.home {
  height: 100%;
  overflow-y: auto;
  padding: 32px 40px;
}
.hero h2 {
  margin: 0 0 6px;
  font-size: 22px;
}
.hero p {
  margin: 0 0 28px;
  color: #6b7280;
  font-size: 14px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(280px, 1fr));
  gap: 20px;
  max-width: 900px;
}
.agent-card {
  display: flex;
  align-items: center;
  gap: 16px;
  background: #fff;
  border: 1px solid #eef2f7;
  border-radius: 14px;
  padding: 20px;
  cursor: pointer;
  transition: all 0.18s ease;
  position: relative;
}
.agent-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(29, 78, 216, 0.12);
  border-color: #c7d7fe;
}
.agent-card.disabled {
  cursor: not-allowed;
  opacity: 0.62;
}
.agent-card.disabled:hover {
  transform: none;
  box-shadow: none;
  border-color: #eef2f7;
}
.card-icon {
  width: 52px;
  height: 52px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  flex-shrink: 0;
}
.card-body {
  flex: 1;
  min-width: 0;
}
.card-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-desc {
  font-size: 13px;
  color: #6b7280;
  line-height: 1.5;
}
.arrow {
  color: #c7d7fe;
}
</style>
