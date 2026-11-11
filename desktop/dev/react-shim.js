// Just enough React for the harness: function components, hooks by tree position, full re-render on
// every state change. Hermes Desktop supplies real React; this file never ships.
export const Fragment = Symbol('Fragment')

export function createElement(type, props, ...children) {
  const all = props && props.children !== undefined && children.length === 0 ? props.children : children
  return { type, props: { ...(props || {}), children: all } }
}
export const jsx = (type, props) => createElement(type, props)
export const jsxs = jsx

const hooks = new Map()
let path = ''
let index = 0
let pending = []
const roots = []
let scheduled = false

function slot(init) {
  const key = `${path}#${index++}`
  if (!hooks.has(key)) hooks.set(key, init())
  return hooks.get(key)
}

function schedule() {
  if (scheduled) return
  scheduled = true
  queueMicrotask(() => { scheduled = false; paint() })
}

export function useState(initial) {
  const cell = slot(() => ({ value: typeof initial === 'function' ? initial() : initial }))
  const set = next => {
    const value = typeof next === 'function' ? next(cell.value) : next
    if (value !== cell.value) { cell.value = value; schedule() }
  }
  return [cell.value, set]
}

export function useEffect(fn, deps) {
  const cell = slot(() => ({ deps: undefined, cleanup: null }))
  const changed = !deps || !cell.deps || deps.length !== cell.deps.length || deps.some((d, i) => !Object.is(d, cell.deps[i]))
  if (changed) {
    cell.deps = deps
    pending.push(() => { if (cell.cleanup) cell.cleanup(); const c = fn(); cell.cleanup = typeof c === 'function' ? c : null })
  }
}

export const useRef = initial => slot(() => ({ current: initial }))
export const useCallback = fn => fn
export function useMemo(fn, deps) {
  const cell = slot(() => ({ deps: undefined, value: undefined }))
  if (!cell.deps || deps.some((d, i) => !Object.is(d, cell.deps[i]))) { cell.deps = deps; cell.value = fn() }
  return cell.value
}

function build(node, at) {
  if (node == null || node === false || node === true) return []
  if (Array.isArray(node)) return node.flatMap((n, i) => build(n, `${at}.${n && n.props && n.props.key != null ? 'k' + n.props.key : i}`))
  if (typeof node !== 'object') return [document.createTextNode(String(node))]
  const { type, props } = node
  if (type === Fragment) return build(props.children, at)
  if (typeof type === 'function') {
    const own = `${at}/${type.name}:${props.key ?? ''}`
    path = own
    index = 0
    const out = type(props)
    return build(out, own)
  }
  const el = type === 'svg' || type === 'path' ? document.createElementNS('http://www.w3.org/2000/svg', type) : document.createElement(type)
  for (const [k, v] of Object.entries(props)) {
    if (k === 'children' || k === 'key' || v == null || v === false) continue
    if (k === 'className') el.setAttribute('class', v)
    else if (k === 'style') Object.assign(el.style, v)
    else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v)
    else if (k === 'value') el.value = v
    else if (k === 'disabled') el.disabled = Boolean(v)
    else el.setAttribute(k, v === true ? '' : v)
  }
  build(props.children, at).forEach(child => el.appendChild(child))
  if (type === 'select' && props.value != null) queueMicrotask(() => { el.value = props.value })
  return [el]
}

function paint() {
  pending = []
  roots.forEach(({ container, element }, i) => {
    const scroll = [...container.querySelectorAll('*')].filter(e => e.scrollTop).map(e => [e.className, e.scrollTop])
    container.replaceChildren(...build(element, `r${i}`))
    scroll.forEach(([cls, top]) => { const e = container.getElementsByClassName(cls)[0]; if (e) e.scrollTop = top })
  })
  const run = pending
  pending = []
  run.forEach(fn => fn())
}

export function render(element, container) {
  roots.push({ container, element })
  paint()
}

export default { createElement, Fragment, useState, useEffect, useRef, useCallback, useMemo }
