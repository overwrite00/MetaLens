const test = require('node:test')
const assert = require('node:assert/strict')
const { spawnTree, killProcessTree } = require('../process-tree')

// A parent that starts a long-running grandchild and reports its pid, mimicking the PyInstaller
// --onefile bootstrap (parent) + real server (child) pair used by the sidecar. On Windows the
// grandchild is detached (own process group), which is the case where terminating only the
// parent leaves it running; on POSIX it shares the parent's group, as the PyInstaller child does.
const PARENT_SCRIPT = `
  const { spawn } = require('child_process')
  const grandchild = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {
    stdio: 'ignore',
    detached: process.platform === 'win32',
  })
  console.log(grandchild.pid)
  setInterval(() => {}, 1000)
`

function isAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

async function waitUntil(predicate, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return true
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  return predicate()
}

function readGrandchildPid(parent) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('grandchild pid not reported')), 10000)
    parent.stdout.once('data', (data) => {
      clearTimeout(timer)
      resolve(Number(String(data).trim()))
    })
  })
}

test('killProcessTree terminates the spawned process and its children', async () => {
  const parent = spawnTree(process.execPath, ['-e', PARENT_SCRIPT], { stdio: ['ignore', 'pipe', 'pipe'] })
  const grandchildPid = await readGrandchildPid(parent)
  assert.ok(isAlive(parent.pid), 'parent should be running')
  assert.ok(isAlive(grandchildPid), 'grandchild should be running')

  killProcessTree(parent)

  assert.ok(await waitUntil(() => !isAlive(parent.pid)), 'parent should be gone')
  assert.ok(await waitUntil(() => !isAlive(grandchildPid)), 'grandchild must not be orphaned')
})

test('killProcessTree ignores a missing child', () => {
  assert.doesNotThrow(() => killProcessTree(null))
  assert.doesNotThrow(() => killProcessTree({ pid: undefined }))
})
