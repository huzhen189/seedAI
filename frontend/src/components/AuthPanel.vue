<script setup lang="ts">
import { ref } from 'vue'
import { useAuth } from '../composables/useAuth'

const { doLogin, doRegister } = useAuth()

const mode = ref<'login' | 'register'>('login')
const username = ref('')
const password = ref('')
const email = ref('')
const err = ref('')
const busy = ref(false)

async function submit() {
  err.value = ''
  if (!username.value.trim() || !password.value) {
    err.value = '请输入用户名和密码'
    return
  }
  busy.value = true
  try {
    if (mode.value === 'login') {
      await doLogin(username.value.trim(), password.value)
    } else {
      await doRegister(username.value.trim(), password.value, email.value.trim() || undefined)
    }
  } catch (e: any) {
    err.value = e?.message || '操作失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth-mask">
    <div class="auth-card">
      <div class="title">SeedAI · {{ mode === 'login' ? '登录' : '注册' }}</div>
      <div class="sub">登录后才能开始对话</div>

      <input v-model="username" placeholder="用户名" autocomplete="username" />
      <input
        v-if="mode === 'register'"
        v-model="email"
        placeholder="邮箱(可选)"
        autocomplete="email"
      />
      <input
        v-model="password"
        type="password"
        placeholder="密码(至少 6 位)"
        autocomplete="current-password"
        @keyup.enter="submit"
      />

      <div v-if="err" class="err">{{ err }}</div>

      <button :disabled="busy" @click="submit">
        {{ busy ? '处理中…' : mode === 'login' ? '登录' : '注册并登录' }}
      </button>

      <div class="switch">
        <template v-if="mode === 'login'">
          还没有账号? <a @click="mode = 'register'">去注册</a>
        </template>
        <template v-else>
          已有账号? <a @click="mode = 'login'">去登录</a>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(244, 246, 250, 0.9);
  z-index: 50;
}
.auth-card {
  width: 320px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 26px 22px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.title {
  font-size: 18px;
  font-weight: 700;
  color: var(--brand);
}
.sub {
  font-size: 12px;
  color: var(--muted);
  margin-top: -6px;
  margin-bottom: 4px;
}
.auth-card input {
  height: 38px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0 12px;
  font-size: 14px;
  outline: none;
}
.auth-card input:focus {
  border-color: var(--brand);
}
.err {
  color: var(--err);
  font-size: 12px;
}
.auth-card button {
  height: 40px;
  border: none;
  border-radius: 8px;
  background: var(--brand);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}
.auth-card button:disabled {
  opacity: 0.6;
  cursor: default;
}
.switch {
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}
.switch a {
  color: var(--brand);
  cursor: pointer;
  font-weight: 600;
}
</style>
