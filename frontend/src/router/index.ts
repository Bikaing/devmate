import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "@/stores/auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/login",
      name: "login",
      component: () => import("@/views/Login.vue"),
      meta: { public: true },
    },
    {
      path: "/",
      component: () => import("@/layouts/BasicLayout.vue"),
      children: [
        { path: "", name: "home", component: () => import("@/views/Home.vue") },
        { path: "chat", name: "chat", component: () => import("@/views/Chat.vue") },
        {
          path: "chat/:conversationId",
          name: "chat-detail",
          component: () => import("@/views/Chat.vue"),
        },
        { path: "review", name: "review", component: () => import("@/views/Review.vue") },
        {
          path: "doc-insight",
          name: "doc-insight",
          component: () => import("@/views/DocInsight.vue"),
        },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

// 全局守卫：未登录只放行 public 路由；已登录访问登录页则回工作台。
router.beforeEach((to) => {
  const auth = useAuthStore();
  if (!to.meta.public && !auth.isLoggedIn) return { name: "login" };
  if (to.name === "login" && auth.isLoggedIn) return { name: "home" };
  return true;
});

export default router;
