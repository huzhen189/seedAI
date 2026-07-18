<script setup lang="ts">
import { ref } from 'vue'
import { useAuth } from '../composables/useAuth'

const emit = defineEmits<{ close: [] }>()
const { doLogin, doRegister } = useAuth()

const mode = ref<'login' | 'register'>('login')
const username = ref('')
const nickname = ref('')
const password = ref('')
const email = ref('')
const showPwd = ref(false)
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
      await doRegister(
        username.value.trim(),
        password.value,
        email.value.trim() || undefined,
        nickname.value.trim() || undefined,
      )
    }
    // 登录成功由父组件 watch(user) 关闭弹窗,此处不重复关闭
  } catch (e: any) {
    err.value = e?.message || '操作失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth-mask" @click.self="emit('close')">
    <div class="auth-card">
      <button class="close-x" @click="emit('close')" aria-label="关闭">×</button>
      <div class="title">SeedAI · {{ mode === 'login' ? '登录' : '注册' }}</div>
      <div class="sub">登录后才能开始对话</div>

      <input v-model="username" placeholder="用户名" autocomplete="username" />

      <input
        v-if="mode === 'register'"
        v-model="nickname"
        placeholder="昵称(可选,默认同用户名)"
        autocomplete="nickname"
      />

      <input
        v-if="mode === 'register'"
        v-model="email"
        placeholder="邮箱(可选)"
        autocomplete="email"
      />

      <div class="pwd-wrap">
        <input
          v-model="password"
          :type="showPwd ? 'text' : 'password'"
          placeholder="密码(至少 6 位)"
          autocomplete="current-password"
          @keyup.enter="submit"
        />
        <button
          type="button"
          class="eye"
          @click="showPwd = !showPwd"
          :title="showPwd ? '隐藏密码' : '显示密码'"
        >
          {{ showPwd ? '🙈' : '👁' }}
        </button>
      </div>

      <div v-if="err" class="err">{{ err }}</div>

      <button class="submit" :disabled="busy" @click="submit">
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
  position: relative;
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
.close-x {
  position: absolute;
  top: 8px;
  right: 12px;
  border: none;
  background: transparent;
  font-size: 20px;
  line-height: 1;
  color: var(--muted);
  cursor: pointer;
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
.pwd-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.pwd-wrap input {
  flex: 1;
  padding-right: 38px;
}
.eye {
  position: absolute;
  right: 6px;
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
}
.err {
  color: var(--err);
  font-size: 12px;
}
.auth-card button.submit {
  height: 40px;
  border: none;
  border-radius: 8px;
  background: var(--brand);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}
.auth-card button.submit:disabled {
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
