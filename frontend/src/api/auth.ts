// 鉴权接口(对应业务服务 /auth/*,文档 §2 / §5)。
// 令牌经 HttpOnly Cookie 下发,前端不持有 token;同源请求浏览器自动携带。

export interface AuthUser {
  id: number
  account: string
  nickname: string
  email: string | null
  role: string
  plan: string
}

export interface UpdateMePayload {
  nickname?: string
  email?: string
  oldPassword?: string
  newPassword?: string
}

async function _json(res: Response) {
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error((e as any).detail || '请求失败')
  }
  return res.json()
}

export async function login(account: string, password: string): Promise<AuthUser> {
  const r = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account, password }),
  })
  return _json(r)
}

export async function register(
  account: string,
  password: string,
  email?: string,
  nickname?: string,
): Promise<AuthUser> {
  const r = await fetch('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      account,
      password,
      email: email || undefined,
      nickname: nickname || undefined,
    }),
  })
  return _json(r)
}

/** 修改当前用户信息(昵称/邮箱/密码);返回更新后的用户态。 */
export async function updateMe(p: UpdateMePayload): Promise<AuthUser> {
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
  return _json(r)
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
    return await r.json()
  } catch {
    return null
  }
}
