<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { User, Lock } from "@element-plus/icons-vue";
import { login, register } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";

const router = useRouter();
const auth = useAuthStore();
const mode = ref<string>("login");
const form = reactive({ username: "", password: "", confirm: "" });
const loading = ref(false);

// el-segmented 选项（登录/注册切换）
const modeOptions = [
  { label: "登录", value: "login" },
  { label: "注册", value: "register" },
];

const isRegister = computed(() => mode.value === "register");

function switchMode(m: string) {
  mode.value = m;
  form.password = "";
  form.confirm = "";
}

async function onSubmit() {
  if (!form.username || !form.password) {
    ElMessage.warning("请输入用户名和密码");
    return;
  }
  if (isRegister.value) {
    if (form.username.length < 3) {
      ElMessage.warning("用户名至少 3 个字符");
      return;
    }
    if (form.password.length < 6) {
      ElMessage.warning("密码至少 6 位");
      return;
    }
    if (form.password !== form.confirm) {
      ElMessage.warning("两次输入的密码不一致");
      return;
    }
  }
  loading.value = true;
  try {
    const t = isRegister.value
      ? await register(form.username, form.password)
      : await login(form.username, form.password);
    auth.setAuth({
      token: t.access_token,
      userId: t.user_id,
      role: t.role,
      username: form.username,
    });
    ElMessage.success(isRegister.value ? "注册成功，已自动登录" : "登录成功");
    router.replace({ name: "home" });
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } };
    ElMessage.error(err.response?.data?.detail || (isRegister.value ? "注册失败" : "登录失败"));
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <el-icon :size="36" color="#1d4ed8"><Cpu /></el-icon>
        <h1>DevMate</h1>
        <p>多 Agent 代码助手</p>
      </div>

      <el-segmented v-model="mode" :options="modeOptions" block class="mode-switch" />

      <el-form size="large" @submit.prevent="onSubmit">
        <el-form-item>
          <el-input
            v-model="form.username"
            placeholder="用户名（3-64 位字母/数字/下划线）"
            :prefix-icon="User"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            type="password"
            :placeholder="isRegister ? '密码（至少 6 位）' : '密码'"
            show-password
            :prefix-icon="Lock"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-form-item v-if="isRegister">
          <el-input
            v-model="form.confirm"
            type="password"
            placeholder="确认密码"
            show-password
            :prefix-icon="Lock"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" @click="onSubmit">
          {{ isRegister ? "注 册" : "登 录" }}
        </el-button>
      </el-form>

      <div class="switch-hint">
        <span v-if="!isRegister">还没有账号？</span>
        <span v-else>已有账号？</span>
        <el-link type="primary" :underline="false" @click="switchMode(isRegister ? 'login' : 'register')">
          {{ isRegister ? "去登录" : "去注册" }}
        </el-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 50%, #3b82f6 100%);
}
.login-card {
  width: 400px;
  background: #fff;
  border-radius: 16px;
  padding: 40px 36px 28px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
}
.brand {
  text-align: center;
  margin-bottom: 22px;
}
.brand h1 {
  margin: 10px 0 4px;
  font-size: 26px;
  color: #1f2937;
}
.brand p {
  margin: 0;
  color: #9ca3af;
  font-size: 13px;
}
.mode-switch {
  margin-bottom: 20px;
}
.submit {
  width: 100%;
  margin-top: 6px;
}
.switch-hint {
  margin-top: 16px;
  text-align: center;
  font-size: 13px;
  color: #6b7280;
  display: flex;
  justify-content: center;
  gap: 4px;
}
</style>
