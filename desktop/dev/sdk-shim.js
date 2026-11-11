// The slice of @hermes/plugin-sdk the Shadow page uses, backed by desktop/dev/serve.py.
const scenario = new URLSearchParams(location.search).get('s') || 'result'
const listeners = new Map()
let profile = 'local-demo'
let connectionId = 'local'
const connections = new Set()
const profiles = new Set()

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }

window.__rpc = []
window.__toasts = []

export const host = {
  state: { profile: { get: () => profile, subscribe: fn => { profiles.add(fn); return () => profiles.delete(fn) } },
    connectionId: { get: () => connectionId, subscribe: fn => { connections.add(fn); return () => connections.delete(fn) } } },
  async request(method, params = {}) {
    window.__rpc.push({ method, params })
    const res = await fetch(`/rpc/${scenario}`, { method: 'POST', body: JSON.stringify({ method, params }) })
    const body = await res.json()
    if (body.error) throw new Error(body.error)
    return body.result
  },
  notify(toast) {
    window.__toasts.push(toast)
    const el = document.getElementById('toast')
    el.textContent = toast.message
    el.hidden = false
  },
  notifyError(error, fallback) { host.notify({ kind: 'error', message: (error && error.message) || fallback }) },
  navigate(path) { window.__navigated = path },
  onEvent(type, fn) {
    if (!listeners.has(type)) listeners.set(type, new Set())
    listeners.get(type).add(fn)
    return () => listeners.get(type).delete(fn)
  }
}

window.__emit = (type, payload = {}) => (listeners.get(type) || []).forEach(fn => fn({ type, payload, session_id: 'local-demo' }))

window.__setProfile = value => { profile = value; profiles.forEach(fn => fn(value)) }

window.__setConnectionId = value => { connectionId = value; connections.forEach(fn => fn(value)) }
