import { defineStore } from "pinia";
import { listConversations, listRepos } from "@/api/codeQa";
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
    setActiveRepo(id: string) {
      this.activeRepoId = id;
    },
  },
});
