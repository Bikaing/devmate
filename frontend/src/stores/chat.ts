import { defineStore } from "pinia";
import { deleteConversation, listConversations, listRepos } from "@/api/codeQa";
import type { Conversation, Repo } from "@/api/types";

// 仓库与会话列表在侧边栏/首页/聊天页共享，集中到一个 store 拉取与缓存。
export const useChatStore = defineStore("chat", {
  state: () => ({
    repos: [] as Repo[],
    conversations: [] as Conversation[],
    activeRepoId: "",
  }),
  getters: {
    // 只有已索引的仓库才能发起问答（与后端 prepare_code_qa 的 409 校验对齐）
    indexedRepos: (s) => s.repos.filter((r) => r.index_status === "indexed"),
    activeRepo: (s) => s.repos.find((r) => r.id === s.activeRepoId) || null,
  },
  actions: {
    async loadRepos() {
      this.repos = await listRepos();
      if (!this.activeRepoId && this.repos.length) {
        const preferred = this.repos.find((r) => r.index_status === "indexed");
        this.activeRepoId = (preferred || this.repos[0]).id;
      }
    },
    async loadConversations() {
      this.conversations = await listConversations();
    },
    // 软删除：后端归档后从本地列表摘除，调用方负责确认与错误提示
    async removeConversation(id: string) {
      await deleteConversation(id);
      this.conversations = this.conversations.filter((c) => c.id !== id);
    },
    setActiveRepo(id: string) {
      this.activeRepoId = id;
    },
  },
});
