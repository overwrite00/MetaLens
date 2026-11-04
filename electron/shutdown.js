// Orderly shutdown, kept free of Electron imports so it can be unit tested.
//
// `quit()` is what the Exit button, the File > Exit menu item and "window closed" all lead to:
// it cancels a running update download, stops the sidecar's whole process tree (releasing its port and
// memory), closes the windows and asks Electron to quit. It is idempotent. If Electron has not exited
// after `forceAfterMs` (a hung renderer, a blocked will-quit), `app.exit()` ends the process anyway
// so nothing is left running.

function createShutdown({ app, stopSidecar, cancelDownload = () => {}, closeWindows = () => {},
                          forceAfterMs = 5000, setTimer = setTimeout }) {
  let started = false

  function quit() {
    if (started) return false
    started = true
    try { cancelDownload() } catch { /* the download is abandoned with the process anyway */ }
    try { stopSidecar() } catch { /* the sidecar is stopped again on will-quit and on process exit */ }
    try { closeWindows() } catch { /* windows are destroyed by app.quit() */ }
    const timer = setTimer(() => app.exit(0), forceAfterMs)
    if (timer && typeof timer.unref === 'function') timer.unref()
    app.quit()
    return true
  }

  return { quit, get started() { return started } }
}

module.exports = { createShutdown }
