<script setup lang="ts">
import { ref } from 'vue'
import { useAuth } from '../composables/useAuth'

const emit = defineEmits<{ close: [] }>()
const { user, doUpdateUser } = useAuth()

// 表单初值取自当前用户态
const nickname = ref(user.value?.nickname || '')
const email = ref(user.value?.email || '')
const oldPassword = ref('')
const newPassword = ref('')
const showPwd = ref(false)
const err = ref('')
const ok = ref('')
const busy = ref(false)

async function save() {
  err.value = ''
  ok.value = ''
  // 仅在用户填写时才传人,避免误清空
  const payload: {
    nickname?: string
    email?: string
    oldPassword?: string
    newPassword?: string
  } = {}
  if (nickname.value.trim() && nickname.value.trim() !== user.value?.nickname) {
    payload.nickname = nickname.value.trim()
  }
  if (email.value.trim() && email.value.trim() !== user.value?.email) {
    payload.email = email.value.trim()
  }
  if (newPassword.value) {
    if (!oldPassword.value) {
      err.value = '修改密码需先填写当前密码'
      return
    }
    payload.oldPassword = oldPassword.value
    payload.newPassword = newPassword.value
  }

  if (Object.keys(payload).length === 0) {
    emit('close')
    return
  }

  busy.value = true
  try {
    const updated = await doUpdateUser(payload)
    ok.value = '已保存'
    // 同步表单(可能昵称被服务端规范化)
    nickname.value = updated.nickname || ''
    email.value = updated.email || ''
    oldPassword.value = ''
    newPassword.value = ''
    setTimeout(() => emit('close'), 600)
  } catch (e: any) {
    err.value = e?.message || '保存失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth-mask" @click.self="emit('close')">
    <div class="auth-card">
      <button class="close-x" aria-label="关闭" @click="emit('close')">×</button>
      <div class="title">账户设置</div>
      <div class="sub">修改昵称、邮箱或密码</div>

      <label class="field-label">昵称</label>
      <input v-model="nickname" placeholder="昵称" autocomplete="nickname" />

      <label class="field-label">邮箱</label>
      <input v-model="email" placeholder="邮箱(可选)" autocomplete="email" />

      <div class="divider"><span>修改密码(留空则不修改)</span></div>

      <label class="field-label">当前密码</label>
      <div class="pwd-wrap">
        <input
          v-model="oldPassword"
          :type="showPwd ? 'text' : 'password'"
          placeholder="当前密码"
          autocomplete="current-password"
        />
        <button type="button" class="eye" @click="showPwd = !showPwd">
          {{ showPwd ? '🙈' : '👁' }}
        </button>
      </div>

      <label class="field-label">新密码</label>
      <input
        v-model="newPassword"
        :type="showPwd ? 'text' : 'password'"
        placeholder="新密码(至少 6 位)"
        autocomplete="new-password"
      />

      <div v-if="err" class="err">{{ err }}</div>
      <div v-if="ok" class="ok">{{ ok }}</div>

      <button class="submit" :disabled="busy" @click="save">
        {{ busy ? '保存中…' : '保存' }}
      </button>
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
  width: 340px;
  max-height: 90vh;
  overflow: auto;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 26px 22px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
  display: flex;
  flex-direction: column;
  gap: 10px;
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
.field-label {
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
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
.divider {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--muted);
  margin: 4px 0;
}
.divider::before,
.divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
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
.ok {
  color: #16a34a;
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
  margin-top: 4px;
}
.auth-card button.submit:disabled {
  opacity: 0.6;
  cursor: default;
}
</style>
