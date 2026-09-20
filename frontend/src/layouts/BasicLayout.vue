<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import ConversationList from "@/components/ConversationList.vue";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const chat = useChatStore();

const activeMenu = computed(() => {
  if (route.path.startsWith("/chat")) return "/chat";
  if (route.path.startsWith("/review")) return "/review";
  return "/";
});

onMounted(() => {
  chat.loadConversations().catch(() => {});
});

function newChat() {
  router.push({ name: "chat" });
}

function logout() {
  auth.clear();
  router.replace({ name: "login" });
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="248px" class="sidebar">
      <div class="logo">
        <el-icon :size="24"><Cpu /></el-icon>
        <span>DevMate</span>
      </div>

      <el-menu :default-active="activeMenu" router>
        <el-menu-item index="/">
          <el-icon><HomeFilled /></el-icon>
          <span>工作台</span>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon>
          <span>代码问答</span>
        </el-menu-item>
        <el-menu-item index="/review">
          <el-icon><View /></el-icon>
          <span>代码审查</span>
        </el-menu-item>
      </el-menu>

      <div class="conv-section">
        <div class="conv-head">
          <span>最近会话</span>
          <el-button link class="icon-btn" @click="newChat">
            <el-icon><Plus /></el-icon>
          </el-button>
        </div>
        <ConversationList />
      </div>

      <div class="user-bar">
        <el-icon><UserFilled /></el-icon>
        <span class="uname">{{ auth.username || auth.role || "用户" }}</span>
        <el-tooltip content="退出登录" placement="top">
          <el-button link class="icon-btn" @click="logout">
            <el-icon><SwitchButton /></el-icon>
          </el-button>
        </el-tooltip>
      </div>
    </el-aside>

    <el-container class="main-area">
      <el-main class="app-main">
        <router-view v-slot="{ Component }">
          <!-- 只缓存审查页：切走再切回保留表单/进度/结果；其余页面行为不变 -->
          <keep-alive include="ReviewPage">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.icon-btn {
  color: #fff;
}
</style>
