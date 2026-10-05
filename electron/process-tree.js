const { spawn, spawnSync } = require('child_process')

const isWindows = process.platform === 'win32'

// The packaged sidecar is a PyInstaller --onefile binary: a bootstrap process that unpacks the
// real server and runs it as a child. Terminating only the process we spawned orphans that child
// (and the port it listens on), so the whole tree has to be terminated.
//
// POSIX: the spawned process leads its own process group (detached), so the group can be signalled.
// Windows: there are no process groups; taskkill /T walks the tree instead. `detached` is left off
// there because it would open a console window.
function spawnTree(command, args, options = {}) {
  return spawn(command, args, { ...options, detached: !isWindows })
}

function killProcessTree(child) {
  if (!child || child.pid === undefined) return
  if (isWindows) {
    spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore',
    })
    return
  }
  try {
    process.kill(-child.pid, 'SIGTERM')
  } catch {
    child.kill('SIGTERM')
  }
}

module.exports = { spawnTree, killProcessTree }
