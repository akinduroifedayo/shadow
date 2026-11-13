import { host, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS } from '@hermes/plugin-sdk'
import { createElement as h, Fragment, useEffect, useState } from 'react'

const PATH = '/shadow'
const EVENT = 'plugin.shadow-trial.page.changed'
const ACTIVE_POLL_MS = 4000
const IDLE_POLL_MS = 30000

const STALE_PROFILE = new Error('Obsolete profile request')

async function call(method, params = {}, generation = store.generation) {
  if (!store.current(generation)) throw STALE_PROFILE
  try {
    const result = await host.request(`plugin.shadow-trial.${method}`, params)
    if (!store.current(generation)) throw STALE_PROFILE
    if (result?.error) throw Object.assign(new Error(result.error.message), { code: result.error.code })
    return result
  } catch (error) {
    if (!store.current(generation)) throw STALE_PROFILE
    if (error?.message === 'plugin_rpc_failed' && store.page?.labels?.['trial.unavailable']) {
      throw new Error(store.page.labels['trial.unavailable'])
    }
    throw error
  }
}

function reportError(error) {
  if (error !== STALE_PROFILE) host.notify({ id: 'shadow.rpc.error', kind: 'error', message: String(error.message || error), durationMs: 8000 })
}

function profileIdentity() {
  return JSON.stringify([host.state.connectionId?.get() ?? null, host.state.profile.get()])
}

const store = {
  profile: profileIdentity(),
  generation: 0,
  refreshId: 0,
  page: null,
  testId: null,
  error: null,
  mounted: 0,
  version: 0,
  listeners: new Set(),
  lastFetch: 0,
  set(patch) {
    Object.assign(this, patch, { version: this.version + 1 })
    this.listeners.forEach(fn => fn())
  },
  syncProfile() {
    const profile = profileIdentity()
    if (profile === this.profile) return false
    this.refreshId += 1
    this.set({ profile, generation: this.generation + 1, page: null, testId: null, error: null })
    return true
  },
  current(generation) {
    this.syncProfile()
    return generation === this.generation
  },
  select(testId, generation) {
    if (!this.current(generation)) return
    this.testId = testId || null
    return this.refresh()
  },
  async refresh() {
    this.syncProfile()
    const generation = this.generation
    const requestId = ++this.refreshId
    this.lastFetch = Date.now()
    try {
      const page = await call('page.state', this.testId ? { test_id: this.testId } : {}, generation)
      if (requestId === this.refreshId) this.set({ page, error: null })
    } catch (error) {
      if (error !== STALE_PROFILE && requestId === this.refreshId) {
        const message = String(error.message || error)
        const missing = `no test ${this.testId}`
        if (this.testId && (error.code === 'test_not_found' || message === missing || message.endsWith(`: ${missing}`))) {
          this.set({ testId: null, page: null, error: null })
          return this.refresh()
        }
        this.set({ error })
      }
    }
  },
  apply(page, generation = this.generation) {
    if (!this.current(generation)) return
    this.refreshId += 1
    this.set({ page, testId: page.test_id || null, error: null })
  },
  get active() {
    return this.mounted > 0 || Boolean(this.page && this.page.status)
  }
}

function useStore() {
  const [, bump] = useState(0)
  useEffect(() => {
    const fn = () => bump(v => v + 1)
    store.listeners.add(fn)
    return () => store.listeners.delete(fn)
  }, [])
  return store
}

function fill(template, fields) {
  return String(template).replace(/\{(\w+)\}/g, (_, k) => (k in fields ? fields[k] : `{${k}}`))
}

function rich(parts) {
  if (!parts) return null
  return parts.map((p, i) => {
    if (typeof p === 'string') return p
    if (p.b != null) return h('b', { key: i }, p.b)
    if (p.code != null) return h('code', { key: i }, p.code)
    if (p.i != null) return h('i', { key: i }, p.i)
    return h('span', { key: i, className: 'k' }, p.k)
  })
}

function Btn({ b, onAct, className = '', disabled = false }) {
  return h('button', {
    type: 'button',
    className: `btn${b.primary ? ' primary' : ''} ${className}`,
    disabled,
    'data-action': b.id,
    onClick: () => onAct(b)
  }, b.label)
}

async function act(action, extra = {}, generation = store.generation, onStartChanged, onOverride) {
  try {
    const result = await call('act', { action, ...extra }, generation)
    if (!store.current(generation)) return false
    store.apply(result.page, generation)
    if (result.confirm_override) {
      if (onOverride) onOverride(result.confirm_override)
      return false
    }
    if (result.start_card) {
      if (onStartChanged) onStartChanged(result.start_card)
      return false
    }
    if (result.notice) host.notify({ kind: 'info', message: result.notice })
    return true
  } catch (error) {
    reportError(error)
    return false
  }
}

// ---------- top row ----------

function Top({ page, onTry, generation = store.generation }) {
  const top = page.top
  const showWeb = page.mode === 'result'
  return h('header', { className: 'top' },
    h('div', null,
      h('h1', null, top.title),
      h('div', { className: 'sub' }, rich(top.sub))),
    h('div', { className: 'acts' },
      showWeb && h(WebToggle, { web: top.web, compact: true, testId: page.test_id, generation }),
      top.picker && h('label', { className: 'picker' },
        h('select', {
          value: top.picker.current || '',
          onChange: e => store.select(e.target.value, generation)
        }, top.picker.options.map(o => h('option', { key: o.id, value: o.id }, o.label))),
        h(Chevron)),
      page.mode !== 'empty' && h('button', {
        type: 'button', className: `btn${top.try.disabled ? '' : ' primary'}`, disabled: top.try.disabled,
        title: top.try.tip || undefined, 'data-action': 'try', onClick: onTry
      }, top.try.label)))
}

function Chevron() {
  return h('svg', { className: 'chev', viewBox: '0 0 10 10', 'aria-hidden': true }, h('path', { d: 'M2 3.5 5 6.5 8 3.5' }))
}

function WebToggle({ web, compact, testId = null, generation = store.generation }) {
  const toggle = async () => {
    try {
      store.apply(await call('settings.set', { web_lookups: !web.on, test_id: testId }, generation), generation)
    } catch (error) {
      reportError(error)
    }
  }
  return h('button', {
    type: 'button', role: 'switch', 'aria-checked': web.on, className: compact ? 'setting' : 'setting-plain',
    'data-action': 'web', onClick: toggle
  }, h('span', { className: `tog${web.on ? ' on' : ''}`, 'aria-hidden': true }), compact && web.label, compact && ' ', compact && h('b', null, web.word))
}

// ---------- empty ----------

function Empty({ page, onTry, generation = store.generation }) {
  const e = page.empty
  return h('section', { className: 'empty' },
    h('h2', null, e.title),
    h('p', null, e.intro),
    h('ol', null, e.steps.map((s, i) => h('li', { key: i }, s))),
    h('div', { className: 'btns' }, e.buttons.map(b => h(Btn, { key: b.id, b, onAct: b.id === 'try' ? onTry : () => act(b.id, {}, generation) }))),
    h('div', { className: 'setrow' },
      h(WebToggle, { web: { ...page.top.web, on: e.web.on }, generation }),
      h('div', null, h('h3', null, e.web.title), h('p', null, e.web.body))),
    h('p', { className: 'idle' }, e.idle))
}

// ---------- verdict strip ----------

function Bar({ bar, faint, thin }) {
  return h('div', { className: `bar${thin ? ' thin' : ''}${faint ? ' faint' : ''}`, role: 'img', 'aria-label': bar.label },
    bar.won > 0 && h('span', { className: 'w', style: { flex: bar.won } }),
    bar.same > 0 && h('span', { className: 's', style: { flex: bar.same } }),
    bar.lost > 0 && h('span', { className: 'l', style: { flex: bar.lost } }))
}

function Receipt({ v }) {
  return h('div', { className: 'rrow' },
    h('p', { className: 'rcap' }, v.caption),
    h('div', { className: `receipt${v.faint ? ' faint' : ''}` }, v.receipt.map(cell =>
      h('div', { key: cell.key, className: `rc rc-${cell.key}`, 'data-cell': cell.key },
        h('span', { className: 'rl' }, cell.label),
        h('span', { className: 'rv' }, rich(cell.value)),
        h('span', { className: `sub${cell.accent_sub ? ' accent' : ''}` }, cell.sub.length ? cell.sub.join(' ') : ' ')))))
}

function RateBar({ bar }) {
  if (!bar) return null
  return h('div', { className: 'rate' },
    h('div', { className: 'rate-track', role: 'img', 'aria-label': bar.label },
      h('span', { className: 'rate-band', style: { left: `${bar.low}%`, width: `${bar.high - bar.low}%` } }),
      h('span', { className: 'rate-half', style: { left: '50%' } }, h('span', { className: 'rate-mid-label' }, bar.midpoint)),
      h('span', { className: 'rate-point', style: { left: `${bar.point}%` } })),
    h('p', { className: 'rate-label' }, bar.label))
}

function ModelTable({ table }) {
  return h('div', { className: 'model-comparison' },
    h('table', null,
      h('thead', null, h('tr', null, table.columns.map(label => h('th', { key: label, scope: 'col' }, label)))),
      h('tbody', null, table.rows.map((row, i) => h('tr', { key: i }, row.map((value, j) =>
        h(j ? 'td' : 'th', { key: j, scope: j ? undefined : 'row' }, value)))))))
}

function Verdict({ page, testId, generation = store.generation }) {
  const v = page.verdict
  const [maths, setMaths] = useState(false)
  const [confirmation, setConfirmation] = useState(null)
  const onAct = b => act(b.id, { test_id: testId }, generation, undefined,
    prompt => setConfirmation({ ...prompt, action: b.id }))
  const confirm = async () => {
    if (await act(confirmation.action, { test_id: testId, confirm_override: true }, generation)) setConfirmation(null)
  }
  return h(Fragment, null,
    confirmation && h('section', { className: 'override-confirm', role: 'dialog', 'aria-label': confirmation.message },
      h('p', null, confirmation.message),
      h('div', { className: 'btns' },
        h('button', { type: 'button', className: 'btn primary', 'data-action': 'confirm-override', onClick: confirm }, confirmation.confirm),
        h('button', { type: 'button', className: 'btn', 'data-action': 'cancel-override', autoFocus: true, onClick: () => setConfirmation(null) }, confirmation.cancel))),
    h('section', { className: 'strip', 'aria-label': v.headline },
      h('div', null,
        h('h2', { className: 'vheadline' }, v.headline),
        h(RateBar, { bar: v.bar }),
        h(ModelTable, { table: v.table }),
        h('span', { className: 'vword advice' }, v.word),
        v.advice_hint && h('p', { className: 'second advice-hint' }, v.advice_hint),
        h('p', { className: 'vsent' }, v.reason),
        v.second_look && h('p', { className: 'second' }, v.second_look)),
      h('div', null,
        h('p', { className: 'sure' }, v.sure)),
      h('div', { className: 'btns' },
        v.buttons.map(b => h(Btn, { key: b.id, b, onAct })),
        v.quiet && h(Btn, { b: v.quiet, onAct, className: 'quiet' })),
      h(Receipt, { v }),
      v.note && h('p', { className: 'vnote' }, v.note),
      h('div', { className: 'factsline' },
        v.facts.map((f, i) => h('span', { key: i }, f)),
        h('button', { type: 'button', className: 'mtog', 'aria-expanded': maths, 'data-action': 'maths', onClick: () => setMaths(!maths) },
          h('span', { className: `tri${maths ? ' open' : ''}`, 'aria-hidden': true }, '›'), ' ', v.maths.summary))),
    maths && h('section', { className: 'maths', 'aria-label': v.maths.summary },
      h('dl', { className: 'facts' }, v.maths.rows.map((r, i) => h(Fragment, { key: i },
        h('dt', null, r.label),
        h('dd', null, rich(r.value), r.warn && h('span', { className: 'warn' }, ' ', r.warn))))),
      h('p', { className: 'method' }, v.maths.method)))
}

// ---------- turn lists ----------

function Row({ row, selected, onOpen }) {
  const cls = `row ${row.kind}${selected ? ' on' : ''}${row.open ? ' open' : ''}`
  const tag = row.kind === 'more' ? h('span', { className: 'tag' }) :
    ['lost', 'won', 'same', 'pick'].includes(row.kind)
      ? h('span', { className: `tag ${row.kind}${selected && row.kind === 'pick' ? ' cur' : ''}` }, h('i'), row.tag)
      : h('span', { className: `st ${row.kind}` }, h('i'), row.tag)
  return h(row.open ? 'button' : 'div', {
    type: row.open ? 'button' : undefined, className: cls, 'data-turn': row.id || undefined,
    onClick: row.open ? () => onOpen(row.id) : undefined
  }, tag, h('span', { className: 't' }, row.text), h('span', { className: 'd' }, row.day))
}

function TurnList({ page, ui, setUi, onOpen }) {
  const list = page.turns
  const L = page.labels
  const [showSame, setShowSame] = useState(false)
  if (ui.judge3 && list.judge3) {
    const j = list.judge3
    const pos = Math.max(0, j.rows.findIndex(r => r.id === ui.openId)) + 1
    return h('div', { className: 'list' },
      h('div', { className: 'lhead' },
        h('h2', null, j.title), h('span', null, fill(L['desktop.judge3.pos'], { n: pos })),
        h('button', { type: 'button', className: 'btn', 'data-action': 'back', onClick: () => setUi({ judge3: false }) }, L['desktop.judge3.back'])),
      h('div', { className: 'gh' }, j.intro),
      j.rows.map(r => h(Row, { key: r.id, row: r, selected: r.id === ui.openId, onOpen })),
      h('p', { className: 'jnote' }, j.note))
  }
  return h('div', { className: 'list' },
    h('div', { className: 'lhead' },
      h('h2', null, list.title), h('span', null, list.count),
      list.judge3_label && h('button', {
        type: 'button', className: 'btn', 'data-action': 'judge3',
        onClick: () => { setUi({ judge3: true }); onOpen(list.judge3.rows[0].id) }
      }, list.judge3_label)),
    ui.tally && h('div', { className: 'gh tally' }, ui.tally),
    list.groups.map(g => {
      const open = !g.folded || showSame
      return h('div', { key: g.key, className: 'group' },
        g.label && h('div', { className: 'gh' }, g.label,
          g.folded && h('button', { type: 'button', className: 'show', 'data-action': 'show-same', onClick: () => setShowSame(!showSame) },
            showSame ? L['desktop.group.hide'] : L['desktop.group.show'])),
        open && g.rows.map((r, i) => h(Row, { key: r.id || `m${i}`, row: r, selected: r.id && r.id === ui.openId, onOpen })))
    }))
}

// ---------- side by side ----------

function Step({ step }) {
  if (step.kind === 'stop') {
    return h('li', null, h('div', { className: 'stop' },
      h('div', { className: 'lbl' }, step.label),
      step.code && h('pre', null, step.code),
      h('div', { className: 'nr' }, step.note)))
  }
  return h('li', null, rich(step.parts))
}

function Answer({ a, turn, pick, setPick, L }) {
  const chosen = turn.revealed ? a.picked : pick === a.n
  return h('div', { className: `ans${chosen ? ' picked' : ''}`, 'data-answer': a.n },
    h('h3', null, a.title, ' ',
      a.name ? h('span', { className: 'name reveal-in' }, rich(a.name)) : h('span', { className: 'hid' }, a.hidden)),
    h('ol', { className: 'steps' }, a.steps.map((s, i) => h(Step, { key: i, step: s }))),
    h('div', { className: 'said' }, h('span', { className: 'k' }, a.said_label), markdown(a.said)),
    turn.can_pick && h('div', { className: 'pick' },
      h('button', {
        type: 'button', className: 'btn', disabled: turn.revealed, 'data-action': `pick-${a.n}`,
        onClick: () => setPick(a.n)
      }, chosen ? L['desktop.sbs.picked'] : L['desktop.sbs.pick'])))
}

function inlineMarkdown(text) {
  return text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g).map((part, key) => {
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) return h('code', { key }, part.slice(1, -1))
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) return h('strong', { key }, inlineMarkdown(part.slice(2, -2)))
    return part
  })
}

function markdown(text) {
  const lines = String(text || '').split(/\r?\n/)
  const blocks = []
  const item = line => line.match(/^\s*([-*+]|\d+\.)\s+(.+)$/)
  let i = 0
  while (i < lines.length) {
    if (!lines[i].trim()) { i++; continue }
    const key = blocks.length
    if (lines[i].startsWith('```')) {
      const code = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) code.push(lines[i++])
      if (i < lines.length) i++
      blocks.push(h('pre', { key }, h('code', null, code.join('\n'))))
    } else if (item(lines[i])) {
      const first = item(lines[i])
      const ordered = /^\d/.test(first[1])
      const items = []
      while (i < lines.length) {
        const match = item(lines[i])
        if (!match || /^\d/.test(match[1]) !== ordered) break
        items.push(h('li', { key: items.length }, inlineMarkdown(match[2])))
        i++
      }
      blocks.push(h(ordered ? 'ol' : 'ul', { key, start: ordered ? parseInt(first[1], 10) : undefined }, items))
    } else {
      const paragraph = []
      while (i < lines.length && lines[i].trim() && !lines[i].startsWith('```') && !item(lines[i])) paragraph.push(lines[i++])
      blocks.push(h('p', { key }, inlineMarkdown(paragraph.join('\n'))))
    }
  }
  return blocks
}

function SideBySide({ page, turn, ui, setUi, onOpen, order, generation = store.generation }) {
  const L = page.labels
  const [pick, setPick] = useState(null)
  useEffect(() => setPick(null), [turn.id])
  const idx = order.indexOf(turn.id)
  const reveal = async () => {
    try {
      const view = await call('turn.reveal', { test_id: turn.test_id, turn_id: turn.id, pick }, generation)
      setUi({ turn: view, tally: view.judge3 && view.judge3.tally ? view.judge3.tally : ui.tally,
              judge3: view.judge3 && view.judge3.tally ? false : ui.judge3 })
    } catch (error) {
      reportError(error)
    }
  }
  const pos = ui.judge3 && page.turns && page.turns.judge3 ? page.turns.judge3.rows.findIndex(r => r.id === turn.id) : -1
  return h('div', { className: 'sbs' },
    h('div', { className: 'ask' },
      h('div', null,
        h('div', { className: 'q' }, turn.ask),
        h('div', { className: 'm' }, turn.meta, pos >= 0 ? ` · ${fill(L['desktop.sbs.judge3_pos'], { n: pos + 1 })}` : '')),
      h('div', { className: 'nav2' },
        h('button', { type: 'button', className: 'btn', disabled: idx <= 0, onClick: () => onOpen(order[idx - 1]) }, L['desktop.sbs.prev']),
        h('button', { type: 'button', className: 'btn', disabled: idx < 0 || idx >= order.length - 1, onClick: () => onOpen(order[idx + 1]) }, L['desktop.sbs.next']))),
    h('div', { className: 'pair' }, turn.answers.map(a => h(Answer, { key: a.n, a, turn, pick, setPick, L }))),
    turn.can_pick && h('div', { className: 'after2' },
      !turn.revealed && h('button', {
        type: 'button', className: `btn${pick != null ? ' primary' : ''}`, 'data-action': 'reveal', disabled: pick == null, onClick: reveal
      }, L['desktop.sbs.reveal']),
      !turn.revealed && h('button', {
        type: 'button', className: `btn quiet${pick === 'same' ? ' on' : ''}`, 'data-action': 'pick-same', onClick: () => setPick('same')
      }, L['desktop.sbs.same']),
      turn.revealed
        ? h('p', { className: 'judge reveal-in' }, turn.judge)
        : h('span', { className: 'tally' }, L['desktop.sbs.hint']),
      turn.revealed && turn.tally && h('span', { className: 'tally' }, turn.tally)),
    turn.can_pick && h(Fold, { turn, L }))
}

function Fold({ turn, L }) {
  const [open, setOpen] = useState(false)
  const can = Boolean(turn.at_the_time)
  return h('div', { className: 'fold' },
    h('button', { type: 'button', className: 'foldhead', disabled: !can, 'aria-expanded': open, onClick: () => setOpen(!open) },
      h('span', { className: `tri${open ? ' open' : ''}`, 'aria-hidden': true }, '›'), ' ',
      can ? turn.at_the_time.label : L['desktop.sbs.fold'],
      !open && h('span', { className: 'meta' }, L['desktop.sbs.folded'])),
    open && can && h('p', { className: 'orig' }, turn.at_the_time.text))
}

// ---------- progress ----------

function Progress({ page, generation = store.generation }) {
  const p = page.progress
  return h('section', { className: 'prog', 'aria-label': p.heading },
    h('div', null, h('h2', null, p.heading), h('p', null, p.sub)),
    h(Btn, { b: p.stop, onAct: b => act(b.id, { test_id: page.test_id }, generation) }),
    h('div', { className: 'ptrack' }, h('span', { style: { transform: `scaleX(${p.frac})` } })),
    h('div', { className: 'pnotes' }, p.notes.map((n, i) => h('span', { key: i }, n))))
}

function Stopped({ page, generation = store.generation }) {
  const stopped = page.stopped
  return h('section', { className: 'prog' },
    h('p', null, stopped.summary),
    h('div', { className: 'btns' }, stopped.buttons.map(b => h(Btn, {
      key: b.id, b, onAct: button => {
        if (!store.current(generation)) return
        if (button.id === 'turns') return document.querySelector('.shd .list')?.scrollIntoView({ block: 'nearest' })
        return act(button.id, { test_id: page.test_id }, generation)
      }
    }))))
}

function HiddenNote({ hidden }) {
  return h('div', { className: 'hidden-note' }, h('h3', null, hidden.title), hidden.paras.map((t, i) => h('p', { key: i }, t)))
}

// ---------- switch and watch ----------

function Rail({ rail }) {
  const cols = rail.segments === 3 ? '210px 1fr 1fr 1fr 110px' : '210px 1fr 110px'
  const width = rail.fill == null ? '0px' : `calc(210px + (100% - 320px) * ${rail.fill / rail.segments} - 6px)`
  return h('div', { className: 'rail', style: { gridTemplateColumns: cols } },
    h('div', { className: 'track' }),
    h('div', { className: 'fill', style: { width } }),
    rail.nodes.map((n, i) => h('div', { key: i, className: `node ${n.state}` }, h('b', null, n.title), n.sub)))
}

function SwitchPanel({ page, generation = store.generation }) {
  const s = page.switch
  return h(Fragment, null,
    h('section', { className: `switch${s.paused ? ' paused' : ''}`, 'aria-label': s.heading },
      h('div', { className: 'hd' },
        h('div', null, h('h2', null, s.heading), h('p', null, s.sub)),
        h('div', { className: 'btns' }, s.buttons.map(b => h(Btn, { key: b.id, b, onAct: x => act(x.id, s.identity, generation) })))),
      h(Rail, { rail: s.rail }),
      h('div', { className: 'sgrid' },
        h('div', null,
          h('h3', null, s.trouble.title),
          h('table', { className: 'tc' },
            h('thead', null, h('tr', null, s.trouble.cols.map((c, i) => h('th', { key: i }, c)))),
            h('tbody', null, s.trouble.rows.map((r, i) => h('tr', { key: i }, r.map((c, j) => h('td', { key: j }, c)))))),
          s.trouble.note && h('p', { className: 'srule' }, s.trouble.note)),
        h('div', null,
          h('h3', null, s.meters.title),
          s.meters.rows.map((m, i) => h('div', { key: i, className: 'meter' },
            h('span', { className: 'lab' }, m.label),
            h('span', { className: 'trk' }, h('span', { style: { width: `${m.frac * 100}%` } }), h('em')),
            h('span', { className: 'v' }, m.value, ' ', h('span', null, m.of)))),
          h('p', { className: 'srule' }, s.meters.rule)))),
    h('section', { className: 'log', 'aria-label': s.log.title },
      h('h3', null, s.log.title),
      h('table', { className: 'tc' }, h('tbody', null, s.log.rows.map((r, i) =>
        h('tr', { key: i }, h('td', { className: 'when' }, r.when), h('td', null, r.text)))))))
}

// ---------- start card ----------

function StartFlow({ page, onClose, generation = store.generation }) {
  const L = page.labels
  const [stage, setStage] = useState({ step: 'models', models: null, card: null, open: false })
  useEffect(() => {
    call('start.models', {}, generation).then(models => {
      if (store.current(generation)) setStage(s => ({ ...s, models }))
    }, reportError)
  }, [])
  const openCard = async (model, judge) => {
    try {
      const card = await call('start.card', judge ? { model, judge } : { model }, generation)
      if (store.current(generation)) setStage(s => ({ ...s, step: 'card', card, open: false }))
    } catch (error) {
      reportError(error)
    }
  }
  if (stage.step === 'models') {
    return h('section', { className: 'startwrap' }, h('article', { className: 'scard' },
      h('h2', null, L['desktop.models.title']),
      !stage.models ? h('p', { className: 'note' }, L['desktop.start.loading']) :
        stage.models.models.length === 0 ? h('p', { className: 'note' }, L['desktop.models.empty']) :
          h('div', { className: 'mlist', role: 'listbox' }, stage.models.models.map(m =>
            h('button', { key: m.slug, type: 'button', role: 'option', className: 'mopt', 'data-model': m.slug, onClick: () => openCard(m.slug) },
              h('span', null, m.name), h('span', { className: 'who' }, m.provider)))),
      h('div', { className: 'btns' }, h('button', { type: 'button', className: 'btn', onClick: onClose }, L['button.cancel']))))
  }
  const card = stage.card
  const begin = b => {
    if (!store.current(generation)) return
    if (b.id === 'cancel') return onClose()
    const action = b.id === 'compare' ? 'start' : b.id
    act(action, { test_id: page.test_id, model: card.model, judge: card.judge_slug || undefined,
      intent: b.id === 'compare' ? 'compare' : 'switch',
      judge_mode: card.judge_mode, judge_models: card.judge_models, estimate: card.estimate, consent: card.consent
    }, generation, refreshed => setStage(s => ({ ...s, card: refreshed, open: false })))
      .then(ok => ok && action === 'start' && store.current(generation) && onClose())
  }
  return h('section', { className: 'startwrap' }, h('article', { className: 'scard', 'aria-label': card.title },
    h('h2', null, card.title),
    h('p', { className: 'id' }, card.id_line),
    card.notice && h('p', { className: 'notice' }, card.notice),
    h('dl', { className: 'rows' }, card.rows.map(r => h(Fragment, { key: r.key },
      h('dt', null, r.label),
      h('dd', null, rich(r.value),
        r.key === 'judge' && card.judge && h('button', {
          type: 'button', className: 'link', 'data-action': 'judge-change', 'aria-expanded': stage.open,
          onClick: () => setStage(s => ({ ...s, open: !s.open }))
        }, stage.open ? card.judge.close : card.judge.change),
        r.key === 'judge' && card.judge && stage.open && h(JudgeList, { judge: card.judge, onPick: slug => openCard(card.model, slug) }))))),
    card.blocked && h('p', { className: 'blocked' }, card.blocked),
    h('div', { className: 'btns' }, card.buttons.map(b => h(Btn, { key: b.id, b, onAct: begin })))))
}

function JudgeList({ judge, onPick }) {
  return h('div', { className: 'jlist', role: 'listbox', 'aria-label': judge.header },
    h('div', { className: 'hd' }, judge.header),
    judge.options.map(o => h('button', {
      key: o.slug, type: 'button', role: 'option', 'aria-selected': o.selected, className: `jopt${o.selected ? ' on' : ''}`,
      'data-judge': o.slug, onClick: () => onPick(o.slug)
    },
      h('span', { className: 'ck' }, o.selected ? '✓' : ''),
      h('span', null, o.name, o.auto && h('span', { className: 'auto' }, ' ', o.auto), h('span', { className: 'who' }, o.who)),
      h('span', { className: 'c' }, o.cost))),
    judge.more && h('div', { className: 'more' }, rich(judge.more)),
    h('div', { className: 'ft' }, judge.footer))
}

// ---------- page ----------

function ShadowPage() {
  const s = useStore()
  return h(ProfilePage, { key: s.generation, generation: s.generation, page: s.page, error: s.error })
}

function ProfilePage({ generation, page, error }) {
  const [ui, setUiState] = useState({ openId: null, turn: null, judge3: false, tally: null, starting: false })
  const setUi = patch => {
    if (store.current(generation)) setUiState(prev => ({ ...prev, ...patch }))
  }
  useEffect(() => {
    store.mounted += 1
    store.refresh()
    return () => { store.mounted -= 1 }
  }, [])
  const testId = page ? page.test_id : null
  useEffect(() => { setUi({ openId: null, turn: null, judge3: false, tally: null }) }, [testId, page && page.mode])
  if (error && !page) return h('div', { className: 'shd' }, h('p', { className: 'err' }, String(error.message || error)))
  if (!page) return h('div', { className: 'shd' })

  const openTurn = async id => {
    if (!id || !store.current(generation)) return
    const tid = page.test_id
    setUi({ openId: id })
    try {
      setUi({ openId: id, turn: await call('turn.open', { test_id: tid, turn_id: id }, generation) })
    } catch (error) {
      reportError(error)
    }
  }
  const order = page.turns
    ? (ui.judge3 && page.turns.judge3 ? page.turns.judge3.rows : page.turns.groups.flatMap(g => g.rows)).filter(r => r.open).map(r => r.id)
    : []
  const onTry = () => setUi({ starting: true })

  let body
  if (ui.starting) {
    body = h(StartFlow, { page, generation, onClose: () => setUi({ starting: false }) })
  } else if (page.mode === 'empty') {
    body = h(Empty, { page, onTry, generation })
  } else if (page.mode === 'switch' || page.mode === 'watch') {
    body = h(SwitchPanel, { page, generation })
  } else {
    const right = ui.turn
      ? h(SideBySide, { page, turn: ui.turn, ui, setUi, onOpen: openTurn, order, generation })
      : page.mode === 'progress'
        ? h('div', { className: 'sbs' }, h(HiddenNote, { hidden: page.progress.hidden }))
        : h('div', { className: 'sbs' }, h('p', { className: 'hint' }, page.labels['desktop.sbs.open_hint']))
    body = h(Fragment, null,
      page.mode === 'result' ? h(Verdict, { key: testId, page, testId, generation })
        : page.mode === 'stopped' ? h(Stopped, { page, generation }) : h(Progress, { page, generation }),
      h('div', { className: 'lower' }, h(TurnList, { page, ui, setUi, onOpen: openTurn }), right))
  }
  return h('div', { className: 'shd', 'data-mode': page.mode }, h(Top, { page, onTry, generation }), body)
}

function StatusItem() {
  const s = useStore()
  const status = s.page && s.page.status
  const generation = s.generation
  if (!status) return null
  return h('button', { type: 'button', className: 'shd-status', onClick: () => {
    if (!store.current(generation)) return
    store.select(null, generation)
    host.navigate(PATH)
  } }, status)
}

// ---------- styles ----------

const CSS = `
.shd, .shd-status {
  --shd-bg: #ffffff; --shd-fg: #1f2328; --shd-fg2: #424a53; --shd-muted: #656d76; --shd-card: #f6f8fa;
  --shd-primary: #0053fd; --shd-accent: #e3edff; --shd-border: #d0d7de; --shd-soft: #e4e8ec;
  --shd-won: #0053fd; --shd-same: #c8d1da; --shd-lost: #32383f;
  --shd-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, sans-serif;
  --shd-mono: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
  --shd-ease: cubic-bezier(0.23, 1, 0.32, 1);
}
.shd { container-type: inline-size; height: 100%; overflow: auto; background: var(--shd-bg); color: var(--shd-fg);
  font-family: var(--shd-sans); font-size: 13px; line-height: 1.5; -webkit-font-smoothing: antialiased; display: flex; flex-direction: column; }
.shd * { box-sizing: border-box; }
.shd b { font-weight: 600; }
.shd code, .shd pre { font-family: var(--shd-mono); font-size: 0.92em; }
.shd .k { color: var(--shd-muted); }
.shd button { font: inherit; color: inherit; }
.shd .btns { display: flex; flex-wrap: wrap; gap: 8px; }
.shd .btn { appearance: none; font-size: 13px; font-weight: 500; height: 30px; padding: 0 12px; border-radius: 4px;
  border: 1px solid var(--shd-border); background: var(--shd-card); color: var(--shd-fg); display: inline-flex; align-items: center;
  justify-content: center; gap: 6px; white-space: nowrap; font-variant-numeric: tabular-nums; cursor: pointer; }
.shd .btn.primary { background: var(--shd-primary); border-color: var(--shd-primary); color: #fff; }
.shd .btn.quiet { background: transparent; border-color: transparent; color: var(--shd-primary); padding: 0 8px; }
.shd .btn.quiet.on { background: var(--shd-accent); }
.shd .btn:disabled { opacity: .55; cursor: default; }
.shd .btn:focus-visible, .shd button:focus-visible { outline: 2px solid var(--shd-primary); outline-offset: 2px; }
.shd .bar { display: flex; gap: 2px; height: 10px; }
.shd .bar.thin { height: 6px; margin-top: 8px; }
.shd .bar > span { display: block; height: 100%; }
.shd .bar > span:first-child { border-radius: 2px 0 0 2px; }
.shd .bar > span:last-child { border-radius: 0 2px 2px 0; }
.shd .bar .w { background: var(--shd-won); } .shd .bar .s { background: var(--shd-same); } .shd .bar .l { background: var(--shd-lost); }
.shd .bar.faint .w { background: #a9c3fb; } .shd .bar.faint .s { background: #e1e6eb; } .shd .bar.faint .l { background: #a3abb4; }

.shd .top { display: flex; align-items: flex-start; gap: 16px; padding: 18px 28px 14px; border-bottom: 1px solid var(--shd-soft); }
.shd .top h1 { font-size: 17px; line-height: 24px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
.shd .top .sub { color: var(--shd-muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
.shd .top .sub b { color: var(--shd-fg); font-weight: 500; }
.shd .top > div { min-width: 0; max-width: 100%; }
.shd .top .acts { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.shd .picker { max-width: 100%; min-width: 0; position: relative; height: 30px; display: inline-flex; align-items: center; }
.shd .picker select { max-width: 100%; min-width: 0; appearance: none; height: 30px; border: 1px solid var(--shd-border); border-radius: 4px; padding: 0 26px 0 10px;
  background: var(--shd-bg); color: var(--shd-fg); font: inherit; font-size: 13px; font-variant-numeric: tabular-nums; cursor: pointer; }
.shd .chev { position: absolute; right: 9px; width: 10px; height: 10px; stroke: var(--shd-muted); fill: none; stroke-width: 1.6; pointer-events: none; }
.shd .setting { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; color: var(--shd-fg2); height: 30px; white-space: nowrap;
  background: none; border: 0; padding: 0; cursor: pointer; }
.shd .setting b { color: var(--shd-fg); font-weight: 500; }
.shd .setting-plain { background: none; border: 0; padding: 0; margin-top: 2px; cursor: pointer; height: 16px; }
.shd .tog { position: relative; display: inline-block; width: 28px; height: 16px; border-radius: 8px; background: var(--shd-border); flex: none; transition: background-color 150ms ease-out; }
.shd .tog::after { content: ""; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%; background: #fff; transition: transform 150ms var(--shd-ease); }
.shd .tog.on { background: var(--shd-primary); }
.shd .tog.on::after { transform: translateX(12px); }

.shd .strip { display: grid; grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.15fr) auto; gap: 32px; align-items: start;
  padding: 20px 28px 18px; border-bottom: 1px solid var(--shd-soft); }
.shd .strip > div { min-width: 0; }
.shd .override-confirm p { white-space: pre-line; }
.shd .override-confirm { margin: 20px 28px; padding: 16px; border: 1px solid var(--shd-primary); border-radius: 6px; background: var(--shd-bg); }
.shd .vheadline { font-size: 25px; line-height: 1.3; font-weight: 600; margin: 0 0 18px; max-width: 820px; }
.shd .advice { display: inline-block; font-size: 12px; padding: 3px 8px; border-radius: 4px; background: var(--shd-soft); }
.shd .rate { max-width: 640px; margin: 20px 0 12px; }
.shd .rate-track { height: 18px; position: relative; background: var(--shd-soft); border-radius: 4px; }
.shd .rate-track span { position: absolute; display: block; }
.shd .rate-band { height: 100%; background: #a9c3fb; border-radius: 3px; }
.shd .rate-half { height: 26px; top: -4px; border-left: 2px solid var(--shd-muted); }
.shd .rate-mid-label { top: -14px; left: 0; transform: translateX(-50%); font-size: 10px; color: var(--shd-muted); }
.shd .rate-point { height: 24px; top: -3px; width: 5px; transform: translateX(-50%); background: var(--shd-primary); border-radius: 2px; }
.shd .rate-label { font-size: 12px; color: var(--shd-muted); margin: 9px 0 0; }
.shd .model-comparison { overflow-x: auto; margin: 14px 0; }
.shd .model-comparison table { border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
.shd .model-comparison th, .shd .model-comparison td { text-align: left; padding: 5px 24px 5px 0; white-space: nowrap; }
.shd .model-comparison thead th { color: var(--shd-muted); font-size: 11px; font-weight: 500; }
.shd .vsent { font-size: 14px; line-height: 1.45; margin: 6px 0 0; max-width: 56ch; text-wrap: pretty; }
.shd .second { font-size: 13px; color: var(--shd-fg2); margin: 6px 0 0; }
.shd .strip .bar.thin { margin-top: 10px; height: 12px; }
.shd .sure { font-size: 13px; color: var(--shd-fg2); margin: 10px 0 0; max-width: 72ch; text-wrap: pretty; }
.shd .strip .btns { flex-direction: column; align-items: stretch; width: 176px; }
.shd .rrow { grid-column: 1 / -1; margin-top: -8px; }
.shd .rcap { margin: 0 0 3px; font-size: 11px; color: var(--shd-muted); }
.shd .receipt { display: grid; grid-template-columns: repeat(5, auto); justify-content: start; font-variant-numeric: tabular-nums;
  border-top: 1px solid var(--shd-soft); border-bottom: 1px solid var(--shd-soft); padding: 7px 0; }
.shd .rc { display: flex; flex-direction: column; gap: 1px; padding: 0 24px 0 12px; border-left: 1px solid var(--shd-soft); min-width: 0; }
.shd .rc:first-child { padding-left: 0; border-left: 0; }
.shd .rl, .shd .rc .sub { font-size: 12px; color: var(--shd-muted); white-space: nowrap; }
.shd .rv { font-size: 14px; color: var(--shd-fg); }
.shd .rv b { font-weight: 700; white-space: nowrap; }
.shd .rc .sub.accent { color: var(--shd-primary); font-weight: 500; }
.shd .receipt.faint .rv, .shd .receipt.faint .sub.accent { color: var(--shd-fg2); }
.shd .vnote { grid-column: 1 / -1; margin: -8px 0 0; font-size: 12px; color: var(--shd-muted); max-width: 90ch; }
.shd .factsline { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 4px 24px; font-size: 12px; color: var(--shd-muted); margin-top: -4px; font-variant-numeric: tabular-nums; }
.shd .mtog { margin-left: auto; color: var(--shd-primary); font-weight: 500; background: none; border: 0; padding: 0; cursor: pointer; font-size: 12px; }
.shd .tri { display: inline-block; transition: transform 160ms var(--shd-ease); }
.shd .tri.open { transform: rotate(90deg); }
.shd .maths { padding: 14px 28px 16px; border-bottom: 1px solid var(--shd-soft); background: var(--shd-bg); }
.shd .maths .facts { display: grid; grid-template-columns: 148px 1fr; gap: 6px 16px; margin: 0; font-size: 13px; max-width: 900px; }
.shd .maths dt { color: var(--shd-muted); font-size: 12px; padding-top: 1px; }
.shd .maths dd { margin: 0; font-variant-numeric: tabular-nums; }
.shd .maths .warn { color: #8a5a00; }
.shd .maths .method { font-size: 12px; color: var(--shd-muted); margin: 12px 0 0; max-width: 900px; }
@container (max-width: 420px) {
  .shd .receipt { grid-template-columns: repeat(6, 1fr); row-gap: 8px; }
  .shd .rc-result, .shd .rc-cost { grid-column: span 3; }
  .shd .rc-speed, .shd .rc-steps, .shd .rc-broken { grid-column: span 2; }
  .shd .rc-speed { padding-left: 0; border-left: 0; }
  .shd .rc .rv b { white-space: nowrap; }
}

.shd .lower { flex: 1; display: grid; grid-template-columns: 392px 1fr; min-height: 0; }
.shd .list { border-right: 1px solid var(--shd-soft); overflow: auto; display: flex; flex-direction: column; padding-bottom: 16px; }
.shd .lhead { display: flex; align-items: baseline; gap: 8px; padding: 14px 20px 8px 28px; }
.shd .lhead h2 { font-size: 13px; font-weight: 600; margin: 0; }
.shd .lhead span { color: var(--shd-muted); font-variant-numeric: tabular-nums; }
.shd .lhead .btn { margin-left: auto; height: 26px; }
.shd .gh { font-size: 12px; color: var(--shd-muted); padding: 10px 20px 4px 28px; font-variant-numeric: tabular-nums; display: flex; gap: 8px; }
.shd .gh.tally { color: var(--shd-fg2); }
.shd .gh .show { margin-left: auto; background: none; border: 0; padding: 0; color: var(--shd-primary); font-size: 12px; font-weight: 500; cursor: pointer; }
.shd .row { display: grid; grid-template-columns: 52px 1fr auto; gap: 8px; align-items: center; height: 32px; padding: 0 20px 0 28px;
  color: var(--shd-fg); width: 100%; background: none; border: 0; text-align: left; font-size: 13px; }
.shd button.row { cursor: pointer; }
.shd[data-mode="progress"] .row { grid-template-columns: 76px 1fr auto; }
.shd .row .tag { font-size: 12px; color: var(--shd-fg2); display: inline-flex; align-items: center; gap: 6px; }
.shd .row .tag i { width: 8px; height: 8px; border-radius: 1px; display: inline-block; }
.shd .row .tag.lost i { background: var(--shd-lost); } .shd .row .tag.won i { background: var(--shd-won); } .shd .row .tag.same i { background: var(--shd-same); }
.shd .row .tag.pick i { background: none; border: 1.5px solid var(--shd-muted); border-radius: 50%; }
.shd .row .tag.pick.cur i { border-color: var(--shd-primary); background: var(--shd-primary); }
.shd .row .t { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.shd .row .d { color: var(--shd-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.shd .row.on { background: var(--shd-accent); box-shadow: inset 2px 0 0 var(--shd-primary); }
.shd .row.same, .shd .row.more, .shd .row.waiting { color: var(--shd-muted); }
.shd .st { font-size: 12px; color: var(--shd-muted); display: inline-flex; align-items: center; gap: 6px; }
.shd .st i { flex: none; width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid var(--shd-muted); display: inline-block; }
.shd .st.done { color: var(--shd-fg2); } .shd .st.done i { background: var(--shd-fg2); border-color: var(--shd-fg2); }
.shd .st.running i, .shd .st.judging i { border-color: var(--shd-primary); background: linear-gradient(90deg, var(--shd-primary) 50%, transparent 50%); }
.shd .jnote { font-size: 12px; color: var(--shd-muted); padding: 12px 20px 0 28px; line-height: 1.5; max-width: 340px; margin: 0; }

.shd .sbs { overflow: auto; padding: 14px 28px 20px; display: flex; flex-direction: column; min-width: 0; }
.shd .sbs .hint { color: var(--shd-muted); margin: 8px 0 0; }
.shd .ask { display: flex; align-items: flex-start; gap: 12px; }
.shd .ask .q { font-size: 15px; line-height: 1.4; font-weight: 500; max-width: 62ch; }
.shd .ask .m { color: var(--shd-muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
.shd .nav2 { margin-left: auto; display: flex; gap: 4px; flex: none; }
.shd .nav2 .btn { height: 26px; }
.shd .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 14px; }
.shd .ans { border: 1px solid var(--shd-border); border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; background: var(--shd-bg); }
.shd .ans.picked { border-color: var(--shd-primary); box-shadow: 0 0 0 1px var(--shd-primary); }
.shd .ans h3 { margin: 0; font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.shd .ans h3 .hid { font-weight: 400; color: var(--shd-muted); font-size: 12px; }
.shd .ans h3 .name { font-weight: 400; font-size: 12px; color: var(--shd-fg2); }
.shd .ans h3 .name b { color: var(--shd-fg); font-weight: 600; }
.shd .steps { list-style: none; margin: 10px 0 0; padding: 0; counter-reset: s; }
.shd .steps li { position: relative; padding: 0 0 8px 22px; counter-increment: s; font-size: 12.5px; line-height: 1.45; }
.shd .steps li::before { content: counter(s); position: absolute; left: 0; top: 1px; width: 15px; height: 15px; border-radius: 50%;
  background: var(--shd-card); border: 1px solid var(--shd-border); font-size: 10px; line-height: 13px; text-align: center; color: var(--shd-muted); font-variant-numeric: tabular-nums; }
.shd .steps code { font-size: 11.5px; }
.shd .stop { margin-top: 2px; border: 1px solid var(--shd-border); border-radius: 4px; background: var(--shd-card); padding: 8px 10px; }
.shd .stop .lbl { font-size: 12px; color: var(--shd-fg2); }
.shd .stop pre { margin: 4px 0; font-size: 11.5px; white-space: pre-wrap; word-break: break-all; color: var(--shd-fg); }
.shd .stop .nr { font-size: 12px; color: var(--shd-muted); }
.shd .said { font-size: 12.5px; border-top: 1px solid var(--shd-soft); padding-top: 8px; margin-top: auto; line-height: 1.45; }
.shd .said .k { display: block; font-size: 12px; margin-bottom: 2px; }
.shd .said p { white-space: pre-wrap; margin: 0 0 8px; }
.shd .said ul, .shd .said ol { padding-left: 22px; margin: 0 0 8px; }
.shd .said pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.shd .ans .pick { margin-top: 10px; }
.shd .ans .pick .btn { height: 28px; }
.shd .ans .pick .btn:active { transform: scale(0.97); transition: transform 120ms ease-out; }
.shd .picked .pick .btn { background: var(--shd-accent); border-color: var(--shd-primary); color: var(--shd-primary); opacity: 1; }
.shd .after2 { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-top: 12px; font-variant-numeric: tabular-nums; }
.shd .after2 .tally { color: var(--shd-muted); margin-left: auto; }
.shd .after2 .judge { margin: 0; color: var(--shd-fg); max-width: 80ch; }
.shd .fold { margin-top: 12px; border-top: 1px solid var(--shd-soft); }
.shd .foldhead { width: 100%; height: 34px; display: flex; align-items: center; gap: 4px; border: 0; border-bottom: 1px solid var(--shd-soft);
  background: none; padding: 0; color: var(--shd-fg2); cursor: pointer; text-align: left; font-size: 13px; }
.shd .foldhead:disabled { cursor: default; }
.shd .foldhead .meta { color: var(--shd-muted); margin-left: auto; font-size: 12px; }
.shd .orig { margin: 8px 0 0; color: var(--shd-fg2); font-size: 12.5px; line-height: 1.5; max-width: 80ch; }
.shd .reveal-in { animation: shd-reveal 180ms var(--shd-ease); }
@keyframes shd-reveal { from { opacity: 0; filter: blur(2px); } to { opacity: 1; filter: blur(0); } }

.shd .prog { padding: 20px 28px 18px; border-bottom: 1px solid var(--shd-soft); display: grid; grid-template-columns: 1fr auto; gap: 8px 32px; align-items: end; }
.shd .prog h2, .shd .switch h2, .shd .empty h2 { font-size: 22px; font-weight: 600; margin: 0; letter-spacing: -0.015em; line-height: 1.2; }
.shd .prog p { margin: 4px 0 0; color: var(--shd-fg2); font-size: 14px; font-variant-numeric: tabular-nums; }
.shd .ptrack { grid-column: 1 / -1; height: 6px; background: var(--shd-card); border: 1px solid var(--shd-border); border-radius: 3px; overflow: hidden; margin-top: 10px; }
.shd .ptrack span { display: block; height: 100%; width: 100%; background: var(--shd-primary); transform-origin: left center; transition: transform 200ms ease-out; }
.shd .pnotes { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 4px 24px; color: var(--shd-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.shd .hidden-note { border: 1px solid var(--shd-border); background: var(--shd-card); border-radius: 6px; padding: 16px 18px; max-width: 560px; margin-top: 8px; }
.shd .hidden-note h3 { margin: 0 0 6px; font-size: 14px; font-weight: 600; }
.shd .hidden-note p { margin: 0 0 8px; color: var(--shd-fg2); line-height: 1.5; }

.shd .switch { padding: 20px 28px 22px; border-bottom: 1px solid var(--shd-soft); }
.shd .switch .hd { display: flex; align-items: flex-start; gap: 16px; }
.shd .switch .hd p { margin: 4px 0 0; color: var(--shd-fg2); font-size: 14px; font-variant-numeric: tabular-nums; }
.shd .switch .hd .btns { margin-left: auto; flex-wrap: nowrap; }
.shd .rail { position: relative; display: grid; margin: 22px 0 6px; }
.shd .rail .track { position: absolute; left: 6px; right: 104px; top: 6px; height: 2px; background: var(--shd-border); }
.shd .rail .fill { position: absolute; left: 6px; top: 6px; height: 2px; background: var(--shd-primary); }
.shd .node { position: relative; padding-top: 22px; font-size: 12px; color: var(--shd-muted); font-variant-numeric: tabular-nums; }
.shd .node::before { content: ""; position: absolute; top: 0; left: 0; width: 14px; height: 14px; border-radius: 50%; background: var(--shd-bg); border: 2px solid var(--shd-border); box-sizing: border-box; }
.shd .node b { display: block; color: var(--shd-fg); font-weight: 600; font-size: 13px; }
.shd .node.done::before { background: var(--shd-primary); border-color: var(--shd-primary); }
.shd .node.now::before { border-color: var(--shd-primary); box-shadow: 0 0 0 3px var(--shd-accent); }
.shd .node.origin::before { border-color: var(--shd-fg2); }
.shd .paused .node.origin::before { background: var(--shd-fg2); }
.shd .sgrid { display: grid; grid-template-columns: 1fr 1.25fr; gap: 32px; margin-top: 18px; }
.shd .sgrid h3, .shd .log h3 { font-size: 13px; font-weight: 600; margin: 0 0 8px; }
.shd table.tc { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
.shd .tc th { text-align: left; font-weight: 400; color: var(--shd-muted); font-size: 12px; padding: 0 0 6px; }
.shd .tc td { padding: 7px 0; border-top: 1px solid var(--shd-soft); }
.shd .sgrid .tc td + td, .shd .sgrid .tc th + th { text-align: right; }
.shd .meter { display: grid; grid-template-columns: 180px 1fr 92px; gap: 12px; align-items: center; height: 30px; font-variant-numeric: tabular-nums; }
.shd .meter .lab { color: var(--shd-fg2); }
.shd .meter .trk { position: relative; height: 6px; background: var(--shd-card); border: 1px solid var(--shd-border); border-radius: 3px; }
.shd .meter .trk span { position: absolute; left: 0; top: -1px; bottom: -1px; background: var(--shd-fg2); border-radius: 3px 0 0 3px; }
.shd .meter .trk em { position: absolute; right: -1px; top: -5px; bottom: -5px; width: 2px; background: var(--shd-fg); }
.shd .meter .v { text-align: right; } .shd .meter .v span { color: var(--shd-muted); }
.shd .srule { color: var(--shd-muted); font-size: 12px; margin: 8px 0 0; max-width: 70ch; }
.shd .log { padding: 16px 28px; }
.shd .log .tc { max-width: 760px; }
.shd .log .when { width: 150px; color: var(--shd-muted); }

.shd .empty { padding: 72px 28px; max-width: 620px; }
.shd .empty h2 { margin-bottom: 8px; }
.shd .empty p { color: var(--shd-fg2); font-size: 14px; line-height: 1.55; margin: 0 0 12px; }
.shd .empty ol { padding-left: 18px; color: var(--shd-fg2); font-size: 14px; line-height: 1.6; margin: 0 0 20px; }
.shd .empty .idle { margin-top: 20px; color: var(--shd-muted); font-size: 12px; }
.shd .setrow { display: flex; gap: 12px; align-items: flex-start; border: 1px solid var(--shd-border); border-radius: 6px; background: var(--shd-card); padding: 12px 14px; margin: 20px 0 0; }
.shd .setrow h3 { margin: 0 0 2px; font-size: 13px; font-weight: 600; }
.shd .setrow p { margin: 0; font-size: 13px; color: var(--shd-fg2); line-height: 1.5; }

.shd .startwrap { padding: 24px 28px 40px; }
.shd .scard { width: 560px; max-width: 100%; border: 1px solid var(--shd-border); border-radius: 6px; background: var(--shd-bg); padding: 20px 20px 16px; font-size: 14px; }
.shd .scard h2 { font-size: 17px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
.shd .scard .id { font-family: var(--shd-mono); font-size: 12px; color: var(--shd-muted); margin: 2px 0 0; }
.shd .scard .note, .shd .scard .notice { font-size: 13px; color: var(--shd-muted); margin: 12px 0 0; }
.shd .scard .notice { color: var(--shd-fg2); }
.shd .scard .blocked { font-size: 13px; color: var(--shd-fg2); margin: 12px 0 0; }
.shd .rows { display: grid; grid-template-columns: 72px 1fr; gap: 8px 16px; margin: 16px 0 0; }
.shd .rows dt { color: var(--shd-muted); font-size: 13px; padding-top: 1px; }
.shd .rows dd { margin: 0; font-variant-numeric: tabular-nums; text-wrap: pretty; }
.shd .link { color: var(--shd-primary); font-weight: 500; font-size: 13px; margin-left: 6px; white-space: nowrap; background: none; border: 0; padding: 0; cursor: pointer; }
.shd .link:hover { text-decoration: underline; }
.shd .jlist { margin: 8px 0 2px; border: 1px solid var(--shd-border); border-radius: 6px; background: var(--shd-bg); box-shadow: 0 4px 14px rgba(31,35,40,.08); overflow: hidden; }
.shd .jlist .hd { font-size: 12px; color: var(--shd-muted); padding: 8px 12px 6px; }
.shd .jopt { display: grid; grid-template-columns: 18px 1fr auto; align-items: baseline; gap: 8px; padding: 7px 12px; font-size: 14px;
  border: 0; border-top: 1px solid var(--shd-soft); width: 100%; background: none; text-align: left; cursor: pointer; }
.shd .jopt .ck { color: var(--shd-primary); font-weight: 700; }
.shd .jopt .who, .shd .mopt .who { color: var(--shd-muted); font-size: 12px; margin-left: 6px; }
.shd .jopt .c { color: var(--shd-muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.shd .jopt.on { background: var(--shd-accent); }
.shd .jopt .auto { color: var(--shd-muted); }
.shd .jlist .more, .shd .jlist .ft { font-size: 12px; color: var(--shd-muted); padding: 7px 12px 9px; border-top: 1px solid var(--shd-soft); }
.shd .jlist .ft { background: var(--shd-card); }
.shd .mlist { margin-top: 12px; border: 1px solid var(--shd-border); border-radius: 6px; overflow: hidden; }
.shd .mopt { display: flex; align-items: baseline; width: 100%; padding: 8px 12px; border: 0; border-top: 1px solid var(--shd-soft); background: none; text-align: left; cursor: pointer; font-size: 14px; }
.shd .mopt:first-child { border-top: 0; }
.shd .scard .btns { margin-top: 16px; }
.shd .err { padding: 28px; color: var(--shd-fg2); }
@media (hover: hover) and (pointer: fine) {
  .shd button.row:hover:not(.on), .shd .jopt:hover:not(.on), .shd .mopt:hover { background: var(--shd-card); }
}
@media (prefers-reduced-motion: reduce) {
  .shd .ptrack span { transition: opacity 150ms ease; }
  .shd .reveal-in { animation: shd-fade 150ms ease-out; }
  .shd .tri, .shd .tog, .shd .tog::after { transition: none; }
}
@keyframes shd-fade { from { opacity: 0; } to { opacity: 1; } }
.shd-status { font-family: var(--shd-sans); font-size: 12px; color: var(--shd-fg); border: 1px solid var(--shd-border); background: var(--shd-bg);
  border-radius: 3px; padding: 1px 8px; cursor: pointer; font-variant-numeric: tabular-nums; white-space: nowrap; }
@container (max-width: 760px) {
  .shd .top { flex-wrap: wrap; }
  .shd .top .acts { margin-left: 0; flex-wrap: wrap; }
  .shd .strip, .shd .prog, .shd .sgrid, .shd .pair, .shd .lower { grid-template-columns: 1fr; }
  .shd .strip { gap: 16px; }
  .shd .strip .btns { flex-direction: row; flex-wrap: wrap; width: auto; }
  .shd .lower { flex: none; }
  .shd .list { border-right: 0; border-bottom: 1px solid var(--shd-soft); overflow: visible; }
  .shd .meter { grid-template-columns: 140px 1fr 72px; }
}
`

export default {
  id: 'shadow',
  name: 'Shadow',
  register(ctx) {
    const style = document.createElement('style')
    style.dataset.plugin = 'shadow'
    style.textContent = CSS
    document.head.appendChild(style)
    ctx.onDispose(() => style.remove())
    ctx.onEvent(EVENT, () => store.refresh())
    ctx.setInterval(() => {
      const due = store.active ? ACTIVE_POLL_MS : IDLE_POLL_MS
      if (Date.now() - store.lastFetch >= due - 50) store.refresh()
    }, ACTIVE_POLL_MS)
    const syncProfile = () => {
      if (store.syncProfile()) store.refresh()
    }
    ctx.onDispose(host.state.profile.subscribe(syncProfile))
    if (host.state.connectionId) ctx.onDispose(host.state.connectionId.subscribe(syncProfile))
    let navLabel
    let disposeNav
    const syncNav = () => {
      const label = store.page?.top?.title || navLabel || this.name
      if (!label || label === navLabel) return
      if (disposeNav) disposeNav()
      navLabel = label
      disposeNav = ctx.registerMany([
        { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label, codicon: 'copy' } }
      ])
    }
    store.listeners.add(syncNav)
    ctx.onDispose(() => {
      store.listeners.delete(syncNav)
      if (disposeNav) disposeNav()
    })
    syncNav()
    store.refresh()
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => h(ShadowPage) },
      { id: 'status', area: STATUSBAR_AREAS.left, order: 140, render: () => h(StatusItem) }
    ])
  }
}
