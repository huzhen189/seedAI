// 鉴权接口(对应业务服务 /auth/*,文档 §2 / §5)。
// 令牌经 HttpOnly Cookie 下发,前端不持有 token;同源请求浏览器自动携带。

export interface AuthUser {
  id: number
  username: string
  email: string
  role: string
  plan: string
}

async function _json(res: Response) {
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error((e as any).detail || '请求失败')
  }
  return res.json()
}

export async function login(username: string, password: string): Promise<AuthUser> {
  const r = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  return _json(r)
}

export async function register(
  username: string,
  password: string,
  email?: string,
): Promise<AuthUser> {
  const r = await fetch('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, email: email || undefined }),
  })
  return _json(r)
}

export async function logout(): Promise<void> {
  await fetch('/auth/logout', { method: 'POST' }).catch(() => {})
}

/** 读取当前登录用户;未登录或出错返回 null(前端据此显示登录层)。 */
export async function fetchMe(): Promise<AuthUser | null> {
  try {
    const r = await fetch('/auth/me')
    if (r.status === 401) return null
    if (!r.ok) return null
    return await r.json()
  } catch {
    return null
  }
}
