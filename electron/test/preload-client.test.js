const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')
const { pathToFileURL } = require('node:url')

test('sandbox preload strips IPC events and returns listener cleanup', () => {
  let api, listener, removed
  const ipcRenderer = {
    invoke: async channel => channel,
    on: (_channel, callback) => { listener = callback },
    removeListener: (channel, callback) => { removed = [channel, callback] },
    removeAllListeners() {},
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'), {
    require: name => {
      assert.equal(name, 'electron')
      return { contextBridge: { exposeInMainWorld: (_name, surface) => { api = surface } }, ipcRenderer }
    },
  })
  let received
  const cleanup = api.onMenuUndo((...args) => { received = args })
  listener({ sender: 'privileged' }, 'payload')
  assert.deepEqual(received, ['payload'])
  cleanup()
  assert.equal(removed[0], 'menu-undo')
  assert.equal(removed[1], listener)
})

test('frontend fails closed without Electron and sends session token in header only', async t => {
  const previousWindow = global.window, previousFetch = global.fetch
  t.after(() => { global.window = previousWindow; global.fetch = previousFetch })
  const url = pathToFileURL(path.join(__dirname, '..', '..', 'frontend', 'src', 'api', 'client.js')).href
  global.window = {}
  let calls = 0
  global.fetch = () => { calls++; assert.fail('Unauthenticated fetch must not occur') }
  const standalone = await import(`${url}?standalone`)
  await assert.rejects(standalone.metalens.health(), /Electron/)
  assert.equal(calls, 0)
  global.window = { electronAPI: { getSidecarConnection: async () => ({ port: 12345, token: 'session-secret' }) } }
  global.fetch = async (target, options) => {
    assert.equal(target, 'http://127.0.0.1:12345/health')
    assert.equal(options.headers.Authorization, 'Bearer session-secret')
    assert.equal(target.includes('secret'), false)
    return { ok: true, json: async () => ({ status: 'ok' }) }
  }
  const authenticated = await import(`${url}?authenticated`)
  assert.deepEqual(await authenticated.metalens.health(), { status: 'ok' })
})
