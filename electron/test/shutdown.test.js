const test = require('node:test')
const assert = require('node:assert/strict')
const { createShutdown } = require('../shutdown')

function setup(overrides = {}) {
  const calls = []
  const timers = []
  const app = { quit: () => calls.push('quit'), exit: code => calls.push(`exit:${code}`) }
  const shutdown = createShutdown({
    app,
    stopSidecar: () => calls.push('sidecar'),
    cancelDownload: () => calls.push('download'),
    closeWindows: () => calls.push('windows'),
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return { unref() {} } },
    ...overrides,
  })
  return { calls, timers, shutdown }
}

test('quit stops everything the app started, then quits', () => {
  const { calls, shutdown } = setup()
  assert.equal(shutdown.quit(), true)
  assert.deepEqual(calls, ['download', 'sidecar', 'windows', 'quit'])
})

test('quit is idempotent', () => {
  const { calls, shutdown } = setup()
  shutdown.quit()
  assert.equal(shutdown.quit(), false)
  assert.equal(calls.filter(c => c === 'sidecar').length, 1)
})

test('a failing step does not stop the others', () => {
  const { calls, shutdown } = setup({ cancelDownload: () => { throw new Error('x') }, stopSidecar: () => { throw new Error('y') } })
  shutdown.quit()
  assert.deepEqual(calls, ['windows', 'quit'])
})

test('the process is ended by force if Electron does not exit in time', () => {
  const { calls, timers, shutdown } = setup({ forceAfterMs: 1234 })
  shutdown.quit()
  assert.equal(timers[0].ms, 1234)
  timers[0].fn()
  assert.equal(calls.at(-1), 'exit:0')
})
