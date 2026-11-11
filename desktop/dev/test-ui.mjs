import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import test from 'node:test'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')
  .replace(/^import .*\n/gm, '')
  .replace('export default {', 'const plugin = {')

function harness() {
  let profile = 'A'
  let connection = 'local'
  let request = async () => ({ mode: 'empty' })
  let states = []
  let slot = 0
  const effects = []
  const subscribers = new Set()
  const connections = new Set()
  const disposers = []
  const contributions = new Map()
  const requests = []
  const errors = []
  const scrolled = []
  const context = vm.createContext({
    Date, Set, Boolean,
    host: {
      request: (method, params) => { requests.push({ method, params, profile, connection }); return request(method, params) },
      notify: toast => { if (toast.kind === 'error') errors.push(toast) }, notifyError: error => errors.push(error), navigate() {},
      state: { profile: { get: () => profile, subscribe: fn => { subscribers.add(fn); return () => subscribers.delete(fn) } },
        connectionId: { get: () => connection, subscribe: fn => { connections.add(fn); return () => connections.delete(fn) } } }
    },
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', STATUSBAR_AREAS: { left: 'left' },
    document: { querySelector: selector => ({ scrollIntoView: () => scrolled.push(selector) }), createElement: () => ({ dataset: {}, remove() {} }), head: { appendChild() {} } },
    h: (type, props, ...children) => ({ type, props: props || {}, children }), Fragment: 'fragment',
    useState: initial => {
      const own = slot++
      if (!(own in states)) states[own] = typeof initial === 'function' ? initial() : initial
      return [states[own], next => { states[own] = typeof next === 'function' ? next(states[own]) : next }]
    },
    useEffect: fn => effects.push(fn)
  })
  vm.runInContext(source + '\nglobalThis.subject = { store, act, Top, WebToggle, SwitchPanel, StartFlow, SideBySide, Answer, ProfilePage, Verdict, Progress, ShadowPage, StatusItem, plugin };', context)
  const api = context.subject
  return {
    ...api, requests, errors, subscribers, connections, contributions, scrolled,
    respond(fn) { request = fn },
    render(name, props = {}, initial = []) { states = [...initial]; slot = 0; effects.length = 0; return api[name](props) },
    state: () => states,
    effects: () => effects.splice(0).map(fn => fn()),
    profile(value, notify = true) { profile = value; if (notify) [...subscribers].forEach(fn => fn(value)) },
    connection(value, notify = true) { connection = value; if (notify) [...connections].forEach(fn => fn(value)) },
    register() { api.plugin.register({ onDispose: fn => disposers.push(fn), onEvent() {}, setInterval() {}, registerMany(items) {
      items.forEach(item => contributions.set(item.id, item))
      return () => items.forEach(item => { if (contributions.get(item.id) === item) contributions.delete(item.id) })
    } }) },
    dispose() { disposers.forEach(fn => fn()) }
  }
}

function find(node, predicate) {
  if (!node || typeof node !== 'object') return null
  if (predicate(node)) return node
  return (node.children || []).flat(Infinity).map(child => find(child, predicate)).find(Boolean)
}
const action = (tree, id) => find(tree, node => node.props.b?.id === id || node.props['data-action'] === id)
const tick = () => new Promise(resolve => setImmediate(resolve))
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const top = { title: 'Shadow', sub: [], web: { on: true }, try: { disabled: false }, picker: { current: 'history', options: [] } }
const card = { model: 'new-model', judge_slug: 'judge', title: 'Try', rows: [], judge: {}, buttons: [{ id: 'start', label: 'Start' }] }
const consent = { step: 'card', card, open: false }

test('a rejected Start card shows the Shadow copy message and keeps the picker open', async () => {
  const h = harness()
  const message = 'A test is already running. Stop it first.'
  h.respond(async () => ({ error: { message } }))
  const tree = h.render('StartFlow', { page: { labels: {} }, onClose() {} },
    [{ step: 'models', models: { models: [{ slug: 'new-model' }] } }])
  find(tree, n => n.props['data-model'] === 'new-model').props.onClick()
  await tick()
  assert.equal(h.errors.length, 1)
  assert.equal(h.errors[0].message, message)
  assert.equal(h.state()[0].step, 'models')
})

test('page errors show the Shadow copy message', async () => {
  const h = harness()
  const message = 'No models to try yet.'
  h.respond(async () => ({ error: { message } }))
  await h.store.refresh()
  const tree = h.render('ProfilePage', { generation: h.store.generation, page: h.store.page, error: h.store.error })
  assert.equal(find(tree, n => n.props.className === 'err')?.children[0], message)
})

test('opaque plugin transport failures show keyed copy and expire', async () => {
  const h = harness()
  h.store.page = { labels: { 'trial.unavailable': 'Shadow is unavailable. Please try again.' } }
  h.respond(async () => { throw new Error('plugin_rpc_failed') })
  await h.act('start')
  assert.equal(h.errors[0].message, 'Shadow is unavailable. Please try again.')
  assert.equal(h.errors[0].durationMs, 8000)
  h.respond(async () => ({ page: { mode: 'progress' } }))
  await h.act('start')
  assert.equal(h.store.error, null)
  assert.equal(h.store.page.mode, 'progress')
})

test('What it said renders bold, inline and fenced code, and lists without HTML', () => {
  const h = harness()
  const said = '**Summary** with `**literal** <tag>`\n\n- First **item**\n- Second item\n\n3. Third\n4. Fourth\n\n```js\n<script>alert(1)</script>\n```\n<img src=x onerror=alert(1)> & text'
  const tree = h.render('Answer', { a: { n: 1, steps: [], said, said_label: 'What it said' }, turn: {}, L: {} })
  assert.equal(find(tree, n => n.type === 'strong')?.children.flat(Infinity)[0], 'Summary')
  assert.equal(find(tree, n => n.type === 'code')?.children[0], '**literal** <tag>')
  assert.equal(find(tree, n => n.type === 'ul')?.children[0].length, 2)
  assert.equal(find(tree, n => n.type === 'ol' && n.props.start === 3)?.children[0].length, 2)
  assert.equal(find(tree, n => n.type === 'pre')?.children[0].children[0], '<script>alert(1)</script>')
  assert.equal(find(tree, n => ['script', 'img'].includes(n.type)), undefined)
  assert.equal(find(tree, n => n.props.dangerouslySetInnerHTML), undefined)
  assert.ok(find(tree, n => n.type === 'p' && n.children.flat(Infinity).includes('<img src=x onerror=alert(1)> & text')))
})

test('profile changes immediately clear the page and history and dispose their subscription', async () => {
  const h = harness()
  h.respond(() => new Promise(() => {}))
  h.register()
  h.store.page = { mode: 'switch', status: 'A running' }
  h.store.testId = 'A-test'
  h.profile('B')
  assert.equal(h.store.page, null)
  assert.equal(h.store.testId, null)
  assert.equal(h.subscribers.size, 1)
  h.dispose()
  assert.equal(h.subscribers.size, 0)
})

test('late page and action responses cannot overwrite another profile', async () => {
  for (const operation of ['refresh', 'act']) {
    const h = harness()
    const old = deferred()
    h.respond(() => old.promise)
    const pending = operation === 'refresh' ? h.store.refresh() : h.act('undo')
    h.profile('B', false)
    h.respond(async () => ({ mode: 'empty', owner: 'B' }))
    await h.store.refresh()
    old.resolve(operation === 'refresh' ? { mode: 'switch', owner: 'A' } : { page: { mode: 'switch', owner: 'A' }, notice: 'old' })
    await pending
    assert.equal(h.store.page.owner, 'B', operation)
  }
})

test('cached setting, picker and Start controls cannot act on the next profile', async () => {
  const h = harness()
  const setting = h.render('WebToggle', { web: { on: true } }).props.onClick
  const picker = find(h.render('Top', { page: { mode: 'result', top } }), n => n.type === 'select').props.onChange
  const start = action(h.render('StartFlow', { page: { labels: {} }, onClose() {} }, [consent]), 'start').props.onAct
  h.profile('B', false)
  h.respond(async () => ({ page: { mode: 'empty' } }))
  await setting()
  picker({ target: { value: 'A-history' } })
  start(card.buttons[0])
  await tick()
  assert.equal(h.requests.length, 0)
  assert.equal(h.store.testId, null)
  assert.equal(h.errors.length, 0)
})

test('late Start card responses cannot repopulate a reset profile', async () => {
  const h = harness()
  const old = deferred()
  h.respond(() => old.promise)
  const tree = h.render('StartFlow', { page: { labels: {} }, onClose() {} }, [{ step: 'models', models: { models: [{ slug: 'A-model' }] } }])
  find(tree, n => n.props['data-model'] === 'A-model').props.onClick()
  h.profile('B', false)
  h.respond(async () => ({ mode: 'empty' }))
  await h.store.refresh()
  old.resolve(card)
  await tick()
  assert.equal(h.state()[0].card, undefined)
})

test('switch action releases history before the next poll', async () => {
  const h = harness()
  h.store.testId = 'old-test'
  h.respond(async (method, params) => method.endsWith('.act')
    ? { page: { mode: 'switch', status: 'running' } }
    : params.test_id ? { mode: 'result', test_id: params.test_id } : { mode: 'switch' })
  await h.act('switch', { test_id: 'old-test' })
  await h.store.refresh()
  assert.equal(h.store.page.mode, 'switch')
  assert.equal(h.store.testId, null)
})

test('the picker and status control can return from history to the current switch and Undo', async () => {
  const h = harness()
  h.store.testId = 'history'
  h.store.page = { mode: 'result', status: 'running', top: { ...top, picker: { current: 'history', options: [{ id: '', label: 'running' }, { id: 'history', label: 'history' }] } } }
  h.respond(async (method, params) => method.endsWith('.act') ? { page: { mode: 'empty' } }
    : params.test_id ? { mode: 'result', status: 'running' } : { mode: 'switch', status: 'running' })
  const picker = find(h.render('Top', { page: h.store.page }), n => n.type === 'select')
  picker.props.onChange({ target: { value: '' } })
  await tick()
  assert.equal(h.store.page.mode, 'switch')
  h.store.testId = 'history'
  h.render('StatusItem').props.onClick()
  await tick()
  assert.equal(h.store.testId, null)
  assert.equal(h.store.page.mode, 'switch')
  await h.act('undo')
  assert.equal(h.store.page.mode, 'empty')
})

test('a rejected Start keeps the consent and selection available for retry', async () => {
  const h = harness()
  let closed = 0
  h.respond(async () => { throw new Error('backend refused') })
  const tree = h.render('StartFlow', { page: { labels: {} }, onClose: () => closed++ }, [consent])
  action(tree, 'start').props.onAct(card.buttons[0])
  await tick()
  assert.equal(closed, 0)
  assert.equal(h.state()[0].card.model, 'new-model')
  assert.equal(h.errors.length, 1)
  h.respond(async () => ({ page: { mode: 'progress' } }))
  action(tree, 'start').props.onAct(card.buttons[0])
  await tick()
  assert.equal(closed, 1)
  assert.equal(h.store.page.mode, 'progress')
})


test('cached stop, switch, Undo, turn and blind reveal controls reject another profile', async () => {
  const h = harness()
  const stop = action(h.render('Progress', { page: { progress: { heading: 'Testing', stop: { id: 'stop_test' }, notes: [] } } }), 'stop_test')
  const verdict = action(h.render('Verdict', { page: { verdict: { word: 'Better', buttons: [{ id: 'switch' }], facts: [], maths: {} } }, testId: 'A-test' }), 'switch')
  const panel = h.render('SwitchPanel', { page: { switch: { buttons: [{ id: 'undo' }], trouble: { cols: [], rows: [] }, meters: { rows: [] }, log: { rows: [] } } } })
  const undo = action(panel, 'undo')
  const turn = { id: 'A-turn', test_id: 'A-test', answers: [], can_pick: true }
  const reveal = action(h.render('SideBySide', { page: { labels: {} }, turn, ui: {}, setUi() {}, onOpen() {}, order: ['A-turn'] }), 'reveal')
  const page = h.render('ProfilePage', { generation: h.store.generation, page: { mode: 'result', test_id: 'A-test', turns: { groups: [] }, labels: {}, top } })
  const open = find(page, node => node.type?.name === 'TurnList').props.onOpen
  h.profile('B', false)
  h.respond(async () => ({ page: { mode: 'empty' } }))
  stop.props.onAct({ id: 'stop_test' })
  verdict.props.onAct({ id: 'switch' })
  undo.props.onAct({ id: 'undo' })
  reveal.props.onClick()
  open('A-turn')
  await tick()
  assert.equal(h.requests.length, 0)
  assert.equal(h.errors.length, 0)
})

test('an A to B to A transition invalidates the first A response and resets page-local state', async () => {
  const h = harness()
  h.respond(() => new Promise(() => {}))
  h.register()
  const oldKey = h.render('ShadowPage').props.key
  const old = deferred()
  h.respond(() => old.promise)
  const pending = h.store.refresh()
  h.profile('B')
  h.profile('A')
  const newTree = h.render('ShadowPage')
  assert.notEqual(newTree.props.key, oldKey)
  assert.equal(newTree.props.page, null)
  h.respond(async () => ({ mode: 'empty', owner: 'new A' }))
  await h.store.refresh()
  old.resolve({ mode: 'switch', owner: 'old A' })
  await pending
  assert.equal(h.store.page.owner, 'new A')
})

test('an old poll cannot replace a newer selection or an action page', async () => {
  for (const next of ['selection', 'action']) {
    const h = harness()
    const old = deferred()
    h.respond(() => old.promise)
    const pending = h.store.refresh()
    h.respond(async () => next === 'action' ? { page: { mode: 'switch' } } : { mode: 'result', test_id: 'new' })
    if (next === 'action') await h.act('switch')
    else {
      h.store.testId = 'new'
      await h.store.refresh()
    }
    old.resolve({ mode: 'result', test_id: 'old' })
    await pending
    assert.equal(h.store.page.mode, next === 'action' ? 'switch' : 'result')
    if (next === 'selection') assert.equal(h.store.page.test_id, 'new')
  }
})

test('stopped tests render their summary and keep Get a result and Delete actionable', async () => {
  const h = harness()
  const page = { mode: 'stopped', test_id: 'stopped-test', top, labels: {}, turns: { groups: [] },
    stopped: { summary: 'Stopped. 22 turns finished.', buttons: [{ id: 'get_result', label: 'Get a result from the 22' }, { id: 'delete_test', label: 'Delete this test' }] } }
  const tree = h.render('ProfilePage', { generation: h.store.generation, page })
  const stopped = find(tree, node => node.type?.name === 'Stopped')
  assert.ok(stopped, 'stopped tests must render a stopped summary without a verdict')
  const summary = stopped.type(stopped.props)
  assert.ok(find(summary, node => node.children?.includes(page.stopped.summary)))
  h.respond(async () => ({ page: { mode: 'result', test_id: 'stopped-test' } }))
  await action(summary, 'get_result').props.onAct(page.stopped.buttons[0])
  assert.equal(h.requests.at(-1).params.test_id, 'stopped-test')
  assert.equal(h.store.page.mode, 'result')
  h.respond(async () => ({ page: { mode: 'empty' } }))
  await action(summary, 'delete_test').props.onAct(page.stopped.buttons[1])
  assert.equal(h.requests.at(-1).params.action, 'delete_test')
  assert.equal(h.store.page.mode, 'empty')
})

test('See the retained turns scrolls to the list without sending an unsupported RPC action', async () => {
  const h = harness()
  const page = { mode: 'stopped', test_id: 'stopped-test', top, labels: {}, turns: { groups: [] },
    stopped: { summary: 'Stopped. 18 turns finished.', buttons: [{ id: 'turns', label: 'See the 18 turns' }] } }
  const tree = h.render('ProfilePage', { generation: h.store.generation, page })
  const stopped = find(tree, node => node.type?.name === 'Stopped')
  const summary = stopped.type(stopped.props)
  h.respond(async () => { throw new Error('Unknown action turns') })
  await action(summary, 'turns').props.onAct(page.stopped.buttons[0])
  assert.equal(h.requests.length, 0)
  assert.equal(h.errors.length, 0)
  assert.deepEqual(h.scrolled, ['.shd .list'])
})

test('backup judging shows its disclosure without a judge Change control', () => {
  const h = harness()
  const backup = { ...card, judge_mode: 'backup_both', judge: null,
    rows: [{ key: 'judge', label: 'Judge', value: ['Backup judging: both models judge each pair blind.'] }] }
  const tree = h.render('StartFlow', { page: { labels: {} }, onClose() {} }, [{ ...consent, card: backup }])
  assert.equal(action(tree, 'judge-change'), undefined)
  assert.ok(action(tree, 'start'))
  assert.ok(find(tree, node => node.children?.flat().includes(backup.rows[0].value[0])))
})

test('Start sends the exact consent card and accepts a refreshed card after mismatch', async () => {
  const h = harness()
  const shown = { ...card, judge_mode: 'third_maker', judge_models: ['judge'], estimate: 1.23, consent: 'seen' }
  const refreshed = { ...shown, judge_models: ['other'], consent: 'refreshed' }
  h.respond(async () => ({ page: { mode: 'empty' }, start_card: refreshed }))
  let closed = false
  const tree = h.render('StartFlow', { page: { labels: {} }, onClose: () => { closed = true } }, [{ ...consent, card: shown }])
  action(tree, 'start').props.onAct(shown.buttons[0])
  await tick()
  const sent = h.requests[0].params
  assert.equal(sent.judge_mode, 'third_maker')
  assert.deepEqual(Array.from(sent.judge_models), ['judge'])
  assert.equal(sent.estimate, 1.23)
  assert.equal(sent.consent, 'seen')
  assert.equal(closed, false)
  assert.equal(h.state()[0].card.consent, 'refreshed')
})

test('switch buttons and Stop submit the identity captured by their displayed page', async () => {
  const h = harness()
  h.respond(async () => ({ page: { mode: 'empty' } }))
  const identity = { switch_id: 'shown-switch', phase: 'running', step: 1 }
  const panel = { heading: 'Switch', buttons: [{ id: 'move_on' }], identity, rail: {},
    trouble: { cols: [], rows: [] }, meters: { rows: [] }, log: { rows: [] } }
  const switchTree = h.render('SwitchPanel', { page: { switch: panel } })
  await action(switchTree, 'move_on').props.onAct({ id: 'move_on' })
  assert.equal(h.requests[0].params.switch_id, 'shown-switch')
  assert.equal(h.requests[0].params.phase, 'running')
  assert.equal(h.requests[0].params.step, 1)
  const progress = { heading: 'Test', stop: { id: 'stop_test' }, notes: [] }
  const testTree = h.render('Progress', { page: { test_id: 'shown-test', progress } })
  await action(testTree, 'stop_test').props.onAct(progress.stop)
  assert.equal(h.requests[1].params.test_id, 'shown-test')
})


test('the reveal button stays disabled until the user picks an answer or same', () => {
  const h = harness()
  const props = { page: { labels: {} }, turn: { id: 't1', test_id: 'test', answers: [], can_pick: true },
    ui: {}, setUi() {}, onOpen() {}, order: ['t1'] }
  assert.equal(action(h.render('SideBySide', props), 'reveal').props.disabled, true)
  for (const pick of [1, 2, 'same']) {
    assert.equal(action(h.render('SideBySide', props, [pick]), 'reveal').props.disabled, false)
  }
})


test('the web lookup toggle submits its displayed history selection', async () => {
  const h = harness()
  const page = { mode: 'result', test_id: 'old-test', top }
  const setting = find(h.render('Top', { page }), n => n.type?.name === 'WebToggle')
  h.respond(async () => ({ ...page, top: { ...top, web: { on: false } } }))
  await h.render('WebToggle', setting.props).props.onClick()
  assert.equal(h.requests.at(-1).params.test_id, 'old-test')
  assert.equal(h.store.testId, 'old-test')
})


test('changing connection with the same profile clears controls and disposes its subscription', () => {
  const h = harness()
  h.respond(() => new Promise(() => {}))
  h.register()
  h.store.page = { mode: 'switch' }
  h.store.testId = 'local-test'
  const generation = h.store.generation
  h.connection('remote')
  assert.equal(h.store.page, null)
  assert.equal(h.store.testId, null)
  assert.notEqual(h.store.generation, generation)
  assert.equal(h.connections.size, 1)
  h.dispose()
  assert.equal(h.connections.size, 0)
})

test('same-name connections reject cached controls and delayed responses', async () => {
  for (const operation of ['refresh', 'act']) {
    const h = harness()
    const old = deferred()
    h.respond(() => old.promise)
    const pending = operation === 'refresh' ? h.store.refresh() : h.act('undo')
    const setting = h.render('WebToggle', { web: { on: true } }).props.onClick
    h.connection('remote', false)
    h.respond(async () => ({ mode: 'empty', owner: 'remote' }))
    await h.store.refresh()
    const count = h.requests.length
    await setting()
    assert.equal(h.requests.length, count)
    old.resolve(operation === 'refresh' ? { mode: 'switch', owner: 'local' }
      : { page: { mode: 'switch', owner: 'local' } })
    await pending
    assert.equal(h.store.page.owner, 'remote')
  }
})


test('the sidebar label follows keyed page copy and is disposed with the plugin', async () => {
  const h = harness()
  h.respond(async () => ({ mode: 'empty', top: { title: 'Shadow from keyed copy' } }))
  h.register()
  await tick()
  assert.equal(h.contributions.get('nav').data.label, 'Shadow from keyed copy')
  h.respond(async () => ({ mode: 'empty', top: { title: 'Updated keyed copy' } }))
  await h.store.refresh()
  assert.equal(h.contributions.get('nav').data.label, 'Updated keyed copy')
  h.dispose()
  assert.equal(h.contributions.has('nav'), false)
})


test('the sidebar remains available when the first backend response fails', async () => {
  const h = harness()
  h.respond(async () => { throw new Error('Backend unavailable') })
  h.register()
  assert.equal(h.contributions.get('nav')?.data.label, 'Shadow')
  await tick()
  assert.equal(h.contributions.get('nav')?.data.path, '/shadow')
  const page = h.render('ProfilePage', { generation: h.store.generation, page: h.store.page, error: h.store.error })
  assert.equal(find(page, node => node.props.className === 'err').children[0], 'Backend unavailable')
  h.dispose()
  assert.equal(h.contributions.has('nav'), false)
})


test('polling recovers when chat deletes the selected historical test', async () => {
  for (const prefix of ['', 'ValueError: ', 'envelope']) {
    const h = harness()
    h.store.testId = 'deleted-test'
    h.store.page = { mode: 'result', test_id: 'deleted-test' }
    h.respond(async (_method, params) => {
      if (params.test_id) {
        if (prefix === 'envelope') return { error: { code: 'test_not_found', message: 'No such test.' } }
        throw new Error(`${prefix}no test deleted-test`)
      }
      return { mode: 'empty', test_id: null }
    })
    await h.store.refresh()
    assert.equal(h.store.testId, null)
    assert.equal(h.store.page.mode, 'empty')
    assert.equal(h.store.error, null)
    assert.equal(h.requests.length, 2)
    assert.equal(h.requests[1].params.test_id, undefined)
  }
})


test('a transient poll failure preserves the selected historical test', async () => {
  const h = harness()
  h.store.testId = 'history'
  h.store.page = { mode: 'result', test_id: 'history' }
  h.respond(async () => { throw new Error('Connection unavailable') })
  await h.store.refresh()
  assert.equal(h.store.testId, 'history')
  assert.equal(h.store.page.test_id, 'history')
  assert.equal(h.requests.length, 1)
})


test('Start sends the viewed history selection when requesting refreshed consent', async () => {
  const h = harness()
  h.respond(async () => ({ page: { mode: 'result', test_id: 'older-test' }, start_card: card }))
  let closed = false
  const tree = h.render('StartFlow', { page: { labels: {}, test_id: 'older-test' }, onClose: () => { closed = true } }, [consent])
  action(tree, 'start').props.onAct(card.buttons[0])
  await tick()
  assert.equal(h.requests[0].params.test_id, 'older-test')
  assert.equal(h.store.testId, 'older-test')
  assert.equal(closed, false)
})

test('a switch warning needs a separate confirm click and Cancel sends nothing', async () => {
  const h = harness()
  const v = { headline: 'Won 1, lost 8 of 30 turns', word: 'Keep your current model',
    table: { columns: [], rows: [] }, buttons: [{ id: 'anyway', label: 'Switch anyway' }], facts: [], maths: {} }
  const page = { mode: 'result', test_id: 'losing', verdict: v }
  const prompt = { message: 'Won 1, lost 8 of 30 turns. Switch anyway?', confirm: 'Switch anyway', cancel: 'Cancel', action: 'anyway' }
  h.respond(async () => ({ page, confirm_override: prompt }))
  const tree = h.render('Verdict', { page, testId: 'losing' })
  await action(tree, 'anyway').props.onAct({ id: 'anyway' })
  assert.equal(h.requests.length, 1)
  assert.equal(h.requests[0].params.confirm_override, undefined)
  assert.equal(h.state()[1].message, prompt.message)
  const dialog = h.render('Verdict', { page, testId: 'losing' }, [false, prompt])
  action(dialog, 'cancel-override').props.onClick()
  assert.equal(h.state()[1], null)
  assert.equal(h.requests.length, 1)
  h.respond(async () => ({ page: { mode: 'switch', test_id: null } }))
  const confirmed = h.render('Verdict', { page, testId: 'losing' }, [false, prompt])
  await action(confirmed, 'confirm-override').props.onClick()
  assert.equal(h.requests[1].params.confirm_override, true)
  assert.equal(h.requests[1].params.test_id, 'losing')
})
