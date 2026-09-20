import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import * as ElementPlusIconsVue from "@element-plus/icons-vue";

import App from "./App.vue";
import router from "./router";
import "./styles/global.css";

const app = createApp(App);

// Element Plus 图标全局注册，模板里直接用 <el-icon><Cpu /></el-icon>
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, comp);
}

app.use(createPinia());
app.use(router);
app.use(ElementPlus);
app.mount("#app");
