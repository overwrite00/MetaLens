const path = require('path')

function handleSquirrelEvent(app, spawn, { platform = process.platform, argv = process.argv, execPath = process.execPath } = {}) {
  if (platform !== 'win32') return false
  const command = argv[1]
  const actions = {
    '--squirrel-install': '--createShortcut',
    '--squirrel-updated': '--createShortcut',
    '--squirrel-uninstall': '--removeShortcut',
  }
  if (command === '--squirrel-obsolete') {
    app.quit()
    return true
  }
  if (!actions[command]) return false
  const updater = path.resolve(path.dirname(execPath), '..', 'Update.exe')
  try {
    const child = spawn(updater, [`${actions[command]}=${path.basename(execPath)}`], {
      detached: true, windowsHide: true, stdio: 'ignore',
    })
    child.once('error', () => app.quit())
    child.once('close', () => app.quit())
  } catch {
    app.quit()
  }
  return true
}

module.exports = { handleSquirrelEvent }
