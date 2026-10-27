const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { isTrustedFrame, secureWindow } = require('../security-policy')
const { handleSquirrelEvent } = require('../squirrel-events')
const { waitForSidecar } = require('../sidecar-health')
const http = require('node:http')

test('IPC only trusts the main frame of the app window at the exact app URL', () => {
  const frame = { url: 'file:///app/frontend/index.html' }
  const contents = { mainFrame: frame }
  const window = { webContents: contents, isDestroyed: () => false }
  assert.equal(isTrustedFrame({ sender: contents, senderFrame: frame }, window, frame.url), true)
  assert.equal(isTrustedFrame({ sender: contents, senderFrame: { url: frame.url } }, window, frame.url), false)
  assert.equal(isTrustedFrame({ sender: {}, senderFrame: frame }, window, frame.url), false)
  assert.equal(isTrustedFrame({ sender: contents, senderFrame: frame }, window, 'file:///other.html'), false)
  assert.equal(isTrustedFrame({ sender: contents, senderFrame: frame }, null, frame.url), false)
})

test('window denies popups, navigation, webviews and permission grants', () => {
  const contents = new EventEmitter()
  let popup, request, check
  contents.setWindowOpenHandler = callback => { popup = callback }
  contents.session = {
    setPermissionRequestHandler: callback => { request = callback },
    setPermissionCheckHandler: callback => { check = callback },
  }
  secureWindow({ webContents: contents })
  assert.deepEqual(popup(), { action: 'deny' })
  let prevented = 0
  contents.emit('will-navigate', { preventDefault: () => prevented++ })
  contents.emit('will-attach-webview', { preventDefault: () => prevented++ })
  contents.emit('will-redirect', { preventDefault: () => prevented++ })
  assert.equal(prevented, 3)
  request(null, 'camera', allowed => assert.equal(allowed, false))
  assert.equal(check(), false)
})

test('Squirrel handles install and errors without allowing normal bootstrap', () => {
  const child = new EventEmitter()
  let quits = 0, calls = 0
  const handled = handleSquirrelEvent({ quit: () => quits++ }, (_exe, args, options) => {
    calls++
    assert.match(args[0], /^--createShortcut=/)
    assert.equal(options.windowsHide, true)
    return child
  }, { platform: 'win32', argv: ['exe', '--squirrel-install'], execPath: 'C:/MetaLens/metalens.exe' })
  assert.equal(handled, true)
  child.emit('error', new Error('missing Update.exe'))
  assert.equal(quits, 1)
  assert.equal(calls, 1)
  assert.equal(handleSquirrelEvent({ quit() {} }, () => assert.fail(), { platform: 'win32', argv: ['exe'] }), false)
})

test('readiness authenticates health rather than accepting an arbitrary TCP listener', async t => {
  const server = http.createServer((request, response) => {
    assert.equal(request.headers.authorization, 'Bearer session-secret')
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ status: 'ok', app: 'MetaLens' }))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => server.close())
  const child = new EventEmitter()
  const port = server.address().port
  assert.equal(await waitForSidecar(child, port, 'session-secret'), port)
  assert.equal(child.listenerCount('exit'), 0)
  assert.equal(child.listenerCount('error'), 0)
})

test('readiness rejects a foreign service and stops polling on child exit', async t => {
  const server = http.createServer((_request, response) => response.end('{"status":"ok","app":"Other"}'))
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => server.close())
  const child = new EventEmitter()
  await assert.rejects(waitForSidecar(child, server.address().port, 'secret', { timeout: 30, interval: 5 }), /timeout/)
  assert.equal(child.listenerCount('exit'), 0)
  const exited = new EventEmitter()
  const promise = waitForSidecar(exited, server.address().port, 'secret')
  exited.emit('exit', 1)
  await assert.rejects(promise, /exited/)
  assert.equal(exited.listenerCount('exit'), 0)
})

test('readiness rejects a spawn error and removes its startup listeners', async () => {
  const child = new EventEmitter()
  const promise = waitForSidecar(child, 1, 'secret')
  child.emit('error', new Error('spawn ENOENT'))
  await assert.rejects(promise, /ENOENT/)
  assert.equal(child.listenerCount('exit'), 0)
  assert.equal(child.listenerCount('error'), 0)
})
