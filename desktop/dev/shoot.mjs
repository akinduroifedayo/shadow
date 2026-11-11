// Drive headless Chrome over CDP against desktop/dev/serve.py: render every state, click through the
// blind pick, reveal, numbers and judge list, check that the DOM shows exactly the RPC payload's strings,
// and save screenshots.  node desktop/dev/shoot.mjs <out_dir> [base_url]
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const OUT = process.argv[2] || 'shots'
const BASE = process.argv[3] || 'http://127.0.0.1:8765'
const PORT = 9333
mkdirSync(OUT, { recursive: true })

const sleep = ms => new Promise(r => setTimeout(r, ms))
const chrome = spawn('google-chrome', ['--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), 'shd-'))}`, 'about:blank'], { stdio: 'ignore' })

let ws
let seq = 0
const waiting = new Map()
function send(method, params = {}) {
  const id = ++seq
  ws.send(JSON.stringify({ id, method, params }))
  return new Promise((resolve, reject) => waiting.set(id, { resolve, reject }))
}
async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw new Error(`${expression}: ${JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text)}`)
  return r.result.value
}
async function settle() {
  let last = -1
  for (let i = 0; i < 40; i++) {
    const n = await evaluate('window.__rpc ? window.__rpc.length : -1')
    if (n === last && n > 0) return sleep(120)
    last = n
    await sleep(120)
  }
}
async function click(selector) {
  const ok = await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return false; el.click(); return true })()`)
  if (!ok) throw new Error(`no element ${selector}`)
  await settle()
}
async function open(scenario, width = 1440, height = 900) {
  await fetch(`${BASE}/reset`, { method: 'POST' })
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 2, mobile: false })
  await send('Page.navigate', { url: `${BASE}/harness.html?s=${scenario}` })
  for (let i = 0; i < 50; i++) {
    await sleep(100)
    if (await evaluate('Boolean(document.querySelector(".shd[data-mode]"))')) break
  }
  await settle()
}
async function shot(name) {
  const { data } = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, 'base64'))
  console.log(`shot ${name}`)
}
async function rpc(scenario, method, params = {}) {
  const r = await fetch(`${BASE}/rpc/${scenario}`, { method: 'POST', body: JSON.stringify({ method: `plugin.shadow.${method}`, params }) })
  return (await r.json()).result
}

const failures = []
function expect(cond, message) {
  if (!cond) failures.push(message)
}
const flat = rich => rich.map(p => (typeof p === 'string' ? p : Object.values(p)[0])).join('')
const norm = s => s.replace(/\s+/g, ' ').trim()

async function checkVerdict(scenario) {
  const page = await rpc(scenario, 'page.state')
  const v = page.verdict
  const dom = await evaluate(`(() => {
    const t = s => [...document.querySelectorAll(s)].map(e => e.textContent)
    return { word: t('.vword')[0], reason: t('.vsent')[0], sure: t('.sure')[0], facts: t('.factsline > span'),
      cells: [...document.querySelectorAll('[data-cell]')].map(e => [e.dataset.cell, e.querySelector('.rv').textContent, e.querySelector('.sub').textContent]),
      bar: document.querySelector('.rate-track')?.getAttribute('aria-label'), headline: t('.vheadline')[0], buttons: t('.strip .btns .btn'),
      sub: t('.top .sub')[0] } })()`)
  expect(dom.word === v.word && dom.reason === v.reason && dom.sure === v.sure, `${scenario}: word/reason/sure`)
  expect(JSON.stringify(dom.facts) === JSON.stringify(v.facts), `${scenario}: facts ${JSON.stringify(dom.facts)}`)
  for (const cell of v.receipt) {
    const got = dom.cells.find(c => c[0] === cell.key)
    expect(got && norm(got[1]) === norm(flat(cell.value)), `${scenario}: receipt ${cell.key} ${got && got[1]} != ${flat(cell.value)}`)
    expect(got && norm(got[2]) === norm(cell.sub.join(' ') || ' '), `${scenario}: receipt sub ${cell.key}`)
  }
  expect(dom.headline === v.headline, `${scenario}: headline`)
  expect(dom.bar === v.bar?.label, `${scenario}: bar`)
  expect(JSON.stringify(dom.buttons) === JSON.stringify([...v.buttons, ...(v.quiet ? [v.quiet] : [])].map(b => b.label)), `${scenario}: buttons`)
  expect(dom.sub === flat(page.top.sub), `${scenario}: top row`)
  await click('[data-action="maths"]')
  const rows = await evaluate(`[...document.querySelectorAll('.maths dd')].map(e => e.textContent)`)
  expect(JSON.stringify(rows.map(norm)) === JSON.stringify(v.maths.rows.map(r => norm(flat(r.value) + (r.warn ? ' ' + r.warn : '')))), `${scenario}: numbers rows`)
  await click('[data-action="maths"]')
}

async function main() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      const target = list.find(t => t.type === 'page')
      if (target) { ws = new WebSocket(target.webSocketDebuggerUrl); break }
    } catch {}
    await sleep(100)
  }
  await new Promise(r => ws.addEventListener('open', r))
  ws.addEventListener('message', e => {
    const msg = JSON.parse(e.data)
    if (msg.id && waiting.has(msg.id)) {
      const w = waiting.get(msg.id)
      waiting.delete(msg.id)
      msg.error ? w.reject(new Error(msg.error.message)) : w.resolve(msg.result)
    }
  })
  await send('Page.enable')
  await send('Runtime.enable')

  await open('empty'); await shot('empty')
  expect(await evaluate(`document.getElementById('status-slot').textContent`) === '', 'empty: no status item while idle')

  await open('progress'); await shot('progress')
  expect(await evaluate(`document.querySelector('.prog h2').textContent`) === 'Testing: 18 of 30 turns done', 'progress heading')
  expect(await evaluate(`document.getElementById('status-slot').textContent`) === 'Shadow · test 18 of 30', 'progress status item')
  const before = await evaluate('window.__rpc.filter(r => r.method === "plugin.shadow.page.state").length')
  await evaluate(`window.__emit('plugin.shadow.page.changed', {mode: 'progress', test_id: 'a3f2'})`)
  await settle()
  const after = await evaluate('window.__rpc.filter(r => r.method === "plugin.shadow.page.state").length')
  expect(after === before + 1, `session event refetches page.state (${before} -> ${after})`)
  await click('[data-turn="t01"]'); await shot('progress-open-turn')
  expect(!(await evaluate(`document.querySelector('.sbs').textContent`)).includes('Hermes 4'), 'progress: opened turn stays blind')

  for (const s of ['result', 'result-better', 'result-worse', 'result-too-close', 'result-not-enough', 'result-backup']) {
    await open(s)
    await checkVerdict(s)
    await shot(s)
  }
  await open('result-worse')
  await click('[data-action="anyway"]'); await shot('switch-anyway-confirm')
  expect(await evaluate('Boolean(document.querySelector("[role=dialog]"))'), 'override dialog shown')
  expect(!(await rpc('result-worse', 'page.state')).switch, 'warning does not switch')
  await click('[data-action="cancel-override"]')
  expect(!(await evaluate('Boolean(document.querySelector("[role=dialog]"))')), 'cancel closes dialog')
  await click('[data-action="anyway"]'); await click('[data-action="confirm-override"]'); await shot('switch-anyway-confirmed')
  expect((await rpc('result-worse', 'page.state')).switch.log.rows.some(r => r.text.includes('Owner override')), 'override is visible in switch history')
  await open('result'); await click('[data-action="maths"]'); await shot('result-numbers')

  await open('result')
  await click('[data-action="judge3"]')
  await click('[data-action="pick-2"]')
  await shot('result-judge3-picked')
  expect(!(await evaluate(`document.querySelector('.lower').textContent`)).match(/Hermes 4|Lost|Won/), 'judge 3: nothing leaks before reveal')
  await click('[data-action="reveal"]'); await shot('result-judge3-revealed')
  expect((await evaluate(`document.querySelector('.after2').textContent`)).includes('The judge picked Answer 2'), 'reveal shows judge reason')
  await click('[data-turn="t07"]'); await click('[data-action="pick-2"]'); await click('[data-action="reveal"]')
  await click('[data-turn="t04"]'); await click('[data-action="pick-1"]'); await click('[data-action="reveal"]')
  await shot('result-judge3-done')
  expect((await evaluate(`document.querySelector('.list').textContent`)).includes('You and the judge agreed on 2 of 3.'), 'judge 3 tally after the third pick')
  await click('[data-action="show-same"]'); await shot('result-list-same-open')

  for (const s of ['switch', 'switch-paused', 'watch', 'watch-tripped']) {
    await open(s)
    const page = await rpc(s, 'page.state')
    const dom = await evaluate(`({ h: document.querySelector('.switch h2').textContent, meters: [...document.querySelectorAll('.meter .v')].map(e => e.textContent),
      cells: [...document.querySelectorAll('.sgrid tbody td')].map(e => e.textContent), status: document.getElementById('status-slot').textContent })`)
    expect(dom.h === page.switch.heading, `${s}: heading`)
    expect(JSON.stringify(dom.meters) === JSON.stringify(page.switch.meters.rows.map(m => `${m.value} ${m.of}`)), `${s}: meters`)
    expect(JSON.stringify(dom.cells) === JSON.stringify(page.switch.trouble.rows.flat()), `${s}: trouble table`)
    expect(dom.status === page.status, `${s}: status item`)
    await shot(s)
  }

  await open('result'); await click('[data-action="try"]'); await shot('start-models')
  await click('[data-model="nousresearch/hermes-4-70b-fp8-2026-08-14"]'); await shot('start-card')
  await click('[data-action="judge-change"]'); await shot('start-card-judge-open')
  await click('[data-judge="moonshot/kimi-k2"]'); await shot('start-card-judge-picked')
  expect((await evaluate(`document.querySelector('.rows').textContent`)).includes('Judged blind by Kimi K2, your pick.'), 'start card: picked judge line')
  expect((await evaluate(`document.querySelector('.rows').textContent`)).includes('About $2.05, capped at $5.00. The judge is about $0.55.'), 'start card: cost follows the judge')

  await open('result', 660, 900); await shot('result-narrow')
  expect(await evaluate(`[...document.querySelectorAll('.vheadline, .rate-track, .model-comparison, .strip .btns')].every(e => e.getBoundingClientRect().right <= innerWidth)`), 'narrow: header and controls fit the page')
  expect(await evaluate(`getComputedStyle(document.querySelector('.receipt')).gridTemplateColumns.split(' ').length`) === 6, 'narrow: receipt becomes the 2 + 3 grid')

  ws.close()
  chrome.kill()
  if (failures.length) {
    console.error(`FAIL ${failures.length}\n` + failures.join('\n'))
    process.exit(1)
  }
  console.log('all DOM checks passed')
}

main().catch(err => { console.error(err); chrome.kill(); process.exit(1) })
