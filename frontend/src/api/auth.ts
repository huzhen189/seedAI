// 鉴权接口(对应业务服务 /auth/*,文档 §2 / §5)。
// 令牌经 HttpOnly Cookie 下发,前端不持有 token;同源请求浏览器自动携带。

export interface AuthUser {
  id: number
  account: string
  nickname: string
  email: string | null
  role: string
  plan: string
  /** 用户级偏好执行模型(user_id 绑定, 后端 preferences.preferred_model)。 */
  preferredModel?: string
}

/**
 * 把后端异常 detail 转为人类可读文案。
 * 后端两类错误体:
 *   - FastAPI 校验失败(422): detail 为 [{loc, msg, type, input}, ...] 数组
 *   - 业务错误(如 409): detail 为 {"code": "ACCOUNT_EXISTS"} 对象
 *   - 普通错误: detail 为字符串
 * 直接 `new Error(detail)` 会把对象/数组 stringify 成 "[object Object]",
 * 这就是登录/注册弹窗里错误文案显示成"对象"的根因。
 */
function detailToText(detail: unknown): string {
  if (!detail) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d: any) => {
        const loc = d?.loc
        const field = Array.isArray(loc)
          ? loc.filter((x: unknown) => typeof x === 'string' && x !== 'body').pop()
          : undefined
        const msg: string = d?.msg ?? ''
        return field ? `${String(field)}：${msg}`.trim() : msg
      })
      .filter(Boolean)
      .join('；')
  }
  if (detail && typeof detail === 'object') {
    const code = (detail as Record<string, unknown>).code
    if (code) {
      const MAP: Record<string, string> = {
        ACCOUNT_EXISTS: '该账号已被注册',
        INVALID_CREDENTIALS: '账号或密码错误',
        ACCOUNT_DISABLED: '账号已被禁用',
        INVALID_TOKEN: '登录已失效，请重新登录',
      }
      return MAP[String(code)] ?? String(code)
    }
    try {
      return JSON.stringify(detail)
    } catch {
      return '请求失败'
    }
  }
  return String(detail)
}

async function _json(res: Response) {
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error(detailToText((e as any).detail) || `请求失败 (${res.status})`)
  }
  return res.json()
}

/**
 * 归一化用户对象: 后端 /auth/login、/auth/register 返回 {user:{...}},
 * 而 /auth/me 直接返回扁平对象; 字段名也不一致(后端用 display_name/tier,
 * 前端用 nickname/plan)。这里统一解包 + 字段映射, 保证 store 里始终是
 * 与 AuthUser 契约一致的扁平对象(TopNav 等直接读 user.account/nickname)。
 */
function normalizeUser(raw: any): AuthUser {
  if (!raw) return raw
  const u = raw.user ? raw.user : raw
  return {
    id: u.id,
    account: u.account,
    nickname: u.nickname ?? u.display_name ?? u.account ?? '',
    email: u.email ?? null,
    role: u.role ?? 'user',
    plan: u.plan ?? u.tier ?? 'free',
    preferredModel: u.preferred_model ?? u.preferredModel ?? 'qwen',
  }
}

/** 保存当前用户的偏好执行模型(仅写自己, 后端白名单校验)。
 * 失败静默:偏好保存是体验增强, 不应阻断发消息等主流程。 */
export async function setPreferredModel(model: string): Promise<void> {
  try {
    const r = await fetch('/auth/me/preferred-model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    })
    if (!r.ok) {
      // 401/400 等: 仅告警, 不影响后续。
      console.warn('[auth] 保存偏好模型失败', r.status)
    }
  } catch (err) {
    console.warn('[auth] 保存偏好模型异常', err)
  }
}

export async function login(account: string, password: string): Promise<AuthUser> {
  const r = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account, password }),
  })
  const data = await _json(r)
  return normalizeUser(data)
}

export async function register(
  account: string,
  password: string,
  email?: string,
  displayName?: string,
): Promise<AuthUser> {
  // 后端 RegisterRequest 字段名为 display_name(必填, 非 nickname),
  // 密码 min_length=6 —— 缺失 display_name 正是注册 422 的根因。
  const r = await fetch('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      account,
      password,
      display_name: displayName || account,
      email: email || undefined,
    }),
  })
  const data = await _json(r)
  return normalizeUser(data)
}

/** 修改当前用户信息(昵称/邮箱/密码);返回更新后的用户态。 */
export async function updateMe(p: {
  nickname?: string
  email?: string
  oldPassword?: string
  newPassword?: string
}): Promise<AuthUser> {
  const r = await fetch('/auth/me', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      nickname: p.nickname,
      email: p.email,
      old_password: p.oldPassword,
      new_password: p.newPassword,
    }),
  })
  const data = await _json(r)
  return normalizeUser(data)
}

export async function logout(): Promise<void> {
  // credentials: 'include' 确保同源下也显式携带 Cookie(删除指令依赖它)。
  // 关键: 必须 await 响应体(res.text),强制浏览器把"清 Cookie 的 Set-Cookie"
  // 完整处理后再返回;否则紧随其后的 location.reload() 可能在 Cookie 落盘前
  // 就抢先导航,导致清 Cookie 被丢弃 → 点退出刷新后又变回登录态(退出无效)。
  try {
    const r = await fetch('/auth/logout', {
      method: 'POST',
      credentials: 'include',
    })
    await r.text().catch(() => {})
  } catch {
    /* 网络失败也继续前端清理,不阻断退出 */
  }
}

/** 读取当前登录用户;未登录或出错返回 null(前端据此显示登录层)。 */
export async function fetchMe(): Promise<AuthUser | null> {
  try {
    // cache:'no-store' 防止刷新后命中 /auth/me 的缓存 200 响应(否则会误判仍登录)
    const r = await fetch('/auth/me', { cache: 'no-store' })
    if (r.status === 401) return null
    if (!r.ok) return null
    const data = await r.json()
    return normalizeUser(data)
  } catch {
    return null
  }
}
