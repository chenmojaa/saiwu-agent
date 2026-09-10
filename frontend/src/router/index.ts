import { createRouter, createWebHistory } from 'vue-router'

const TOKEN_KEY = 'hd_auth_token'

function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
}

function clearToken(): void {
  try { localStorage.removeItem(TOKEN_KEY) } catch {}
}

/** 本地快速检查：token 格式 `userId.token_version.exp.sig`（4 段），
 * 先看是否已过期（毫秒级）。
 *
 * 历史：旧格式是 3 段 `userId.exp.sig`。服务器自 token_version 引入以来
 * 一直拒绝 3 段 token（验证会返回 None），所以这里也直接拒绝 3 段，
 * 避免把 "服务器会拒" 的 token 误判为本地有效导致用户卡在登录页。
 */
function isTokenLocallyValid(token: string | null): boolean {
  if (!token) return false
  const parts = token.split('.')
  if (parts.length === 4) {
    // New format: userId.token_version.exp.sig -- exp is at index 2.
    const exp = Number(parts[2])
    return Number.isFinite(exp) && Date.now() < exp * 1000
  }
  // Any other shape (including legacy 3-segment) is rejected locally;
  // the server-side verify_token() in app/api/auth.py rejects them too.
  return false
}

// 每次页面加载只做一次服务端校验，结果缓存在内存中（导航不重复请求）
let authVerified: boolean | null = null

/** 服务端校验 token 有效性（调 /api/auth/me） */
async function verifyWithServer(token: string): Promise<boolean> {
  try {
    const res = await fetch('/api/auth/me', { headers: { 'X-Auth-Token': token } })
    return res.ok
  } catch {
    // 网络异常时保守放行，后续 API 401 会兜底跳转
    return true
  }
}

async function isAuthed(): Promise<boolean> {
  const token = getToken()
  // 本地过期/格式错误：直接视为未登录，不必请求服务端
  if (!token || !isTokenLocallyValid(token)) {
    clearToken()
    authVerified = false
    return false
  }
  if (authVerified !== null) return authVerified
  authVerified = await verifyWithServer(token)
  if (!authVerified) clearToken()
  return authVerified
}

// 登录成功后调用，重置校验缓存
export function markAuthed(): void {
  authVerified = true
}

export function markLoggedOut(): void {
  authVerified = false
  clearToken()
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/login", name: "login", component: () => import("@/views/LoginView.vue"), meta: { public: true } },
    { path: "/chat", name: "chat", component: () => import("@/views/ChatView.vue") },
    { path: "/chat/:id", name: "chat-id", component: () => import("@/views/ChatView.vue") },
    { path: "/notes", name: "notes", component: () => import("@/views/NotesView.vue") },
    { path: "/skills-mcp", name: "skills-mcp", component: () => import("@/views/SkillsMcpView.vue") },
    { path: "/mcp", redirect: "/skills-mcp" },
    { path: "/settings", redirect: "/chat" },
  ],
})

// 路由守卫：真正校验登录态（本地过期检查 + 服务端验证），未登录一律跳转登录页
router.beforeEach(async (to) => {
  const authed = await isAuthed()
  if (to.meta.public) {
    // 已登录访问登录页时直接进入应用
    if (to.name === "login" && authed) return { path: "/chat" }
    return true
  }
  if (!authed) {
    return { name: "login", query: to.fullPath !== "/" ? { redirect: to.fullPath } : {} }
  }
  return true
})

export default router
