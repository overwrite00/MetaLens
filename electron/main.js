const { app, BrowserWindow, ipcMain, dialog, Menu } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const net = require('net')
const fs = require('fs')
const { randomBytes } = require('crypto')
const { pathToFileURL } = require('url')
const { isTrustedFrame, secureWindow } = require('./security-policy')
const { waitForSidecar } = require('./sidecar-health')
const { handleSquirrelEvent } = require('./squirrel-events')
const { spawnTree, killProcessTree } = require('./process-tree')

// ─────────────────────── Windows/Squirrel install & update events ────────────
// Squirrel (electron-forge maker-squirrel) launches the app with special flags
// during install/uninstall/update to let it create/remove shortcuts. Without
// this early exit, the app boots normally on those events and Squirrel then
// relaunches it once setup is done — visible as GUI+splash flashing then
// reopening. Inlined (rather than the electron-squirrel-startup package) so no
// runtime node_modules need to be shipped in the packaged app.
const squirrelEvent = handleSquirrelEvent(app, spawn)

// ─────────────────────── Privacy: disable Chromium network services ──────────
// Must be called before app.whenReady() to take effect.
app.commandLine.appendSwitch('disable-background-networking')
app.commandLine.appendSwitch('disable-client-side-phishing-detection')
app.commandLine.appendSwitch('disable-component-update')
app.commandLine.appendSwitch('disable-default-apps')
app.commandLine.appendSwitch('disable-domain-reliability')
app.commandLine.appendSwitch('disable-features', 'AutofillServerCommunication,CrashReporting,Reporting')
app.commandLine.appendSwitch('disable-sync')
app.commandLine.appendSwitch('metrics-recording-only')
app.commandLine.appendSwitch('no-first-run')
app.commandLine.appendSwitch('no-pings')
app.commandLine.appendSwitch('safebrowsing-disable-auto-update')

let mainWindow = null
let sidecarProcess = null
let sidecarPort = null
const sidecarToken = randomBytes(32).toString('hex')
const trustedURL = app.isPackaged
  ? pathToFileURL(path.join(__dirname, 'frontend', 'index.html')).href
  : 'http://localhost:5173/'

// ─────────────────────── Port discovery ──────────────────────────────────────

function getAvailablePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port
      srv.close(() => resolve(port))
    })
    srv.on('error', reject)
  })
}

// ─────────────────────── Python sidecar ──────────────────────────────────────

async function startSidecar() {
  sidecarPort = await getAvailablePort()

  // In production: bundled binary sits next to the Electron binary
  // In development: run python main.py directly
  const isDev = !app.isPackaged

  let sidecarBin, sidecarArgs

  if (isDev) {
    const pythonDir = path.join(__dirname, '..', 'python')
    // Prefer the venv Python so all packages are available
    const venvWin  = path.join(pythonDir, '.venv', 'Scripts', 'python.exe')
    const venvUnix = path.join(pythonDir, '.venv', 'bin', 'python')
    if (fs.existsSync(venvWin))       sidecarBin = venvWin
    else if (fs.existsSync(venvUnix)) sidecarBin = venvUnix
    else sidecarBin = process.platform === 'win32' ? 'python' : 'python3'
    sidecarArgs = [path.join(pythonDir, 'main.py'), String(sidecarPort)]
    process.env.PYTHONPATH = pythonDir
  } else {
    const binName = process.platform === 'win32' ? 'metalens-sidecar.exe' : 'metalens-sidecar'
    sidecarBin = path.join(process.resourcesPath, binName)
    sidecarArgs = [String(sidecarPort)]
  }

  sidecarProcess = spawnTree(sidecarBin, sidecarArgs, {
    env: { ...process.env, METALENS_SIDECAR_TOKEN: sidecarToken },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  sidecarProcess.stdout.on('data', data => console.log('[sidecar]', data.toString().trim()))
  sidecarProcess.stderr.on('data', data => console.error('[sidecar]', data.toString().trim()))
  try {
    return await waitForSidecar(sidecarProcess, sidecarPort, sidecarToken)
  } catch (error) {
    stopSidecar()
    throw error
  }
}

function stopSidecar() {
  if (sidecarProcess) {
    killProcessTree(sidecarProcess)
    sidecarProcess = null
  }
}

// ─────────────────────── IPC handlers ────────────────────────────────────────

function setupIPC() {
  const handle = (channel, callback) => ipcMain.handle(channel, (event, ...args) => {
    if (!isTrustedFrame(event, mainWindow, trustedURL)) throw new Error('Untrusted IPC sender')
    return callback(event, ...args)
  })
  handle('get-port', () => sidecarPort)
  handle('get-sidecar-connection', () => ({ port: sidecarPort, token: sidecarToken }))

  handle('get-app-version', () => app.getVersion())

  handle('open-folder-dialog', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openDirectory'],
    })
    return result.canceled ? null : result.filePaths[0]
  })

  handle('open-file-dialog', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile', 'multiSelections'],
    })
    return result.canceled ? [] : result.filePaths
  })

  handle('path-sep', () => path.sep)

  handle('path-join', (_, ...parts) => path.join(...parts))

  handle('path-dirname', (_, p) => path.dirname(p))

  handle('list-drives', () => {
    if (process.platform !== 'win32') return []
    // Return available Windows drive letters
    const drives = []
    for (let i = 65; i <= 90; i++) {
      const drive = String.fromCharCode(i) + ':\\'
      try { fs.accessSync(drive); drives.push(drive) } catch {}
    }
    return drives
  })

  handle('home-dir', () => require('os').homedir())
}

// ─────────────────────── Menu ─────────────────────────────────────────────────

function buildMenu() {
  const template = [
    {
      label: 'File',
      submenu: [
        { label: 'Open Folder…', accelerator: 'CmdOrCtrl+O',
          click: () => mainWindow.webContents.send('menu-open-folder') },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { label: 'Undo', accelerator: 'CmdOrCtrl+Z',
          click: () => mainWindow.webContents.send('menu-undo') },
        { type: 'separator' },
        { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' }, { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        { label: 'About MetaLens',
          click: () => mainWindow.webContents.send('menu-about') },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ─────────────────────── Window ──────────────────────────────────────────────

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0a0e17',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
    icon: path.join(__dirname, '..', 'frontend', 'public', 'icon.png'),
  })

  secureWindow(mainWindow, trustedURL)
  const isDev = !app.isPackaged
  if (isDev) {
    await mainWindow.loadURL('http://localhost:5173')
    // mainWindow.webContents.openDevTools()
  } else {
    await mainWindow.loadFile(
      path.join(__dirname, 'frontend', 'index.html')
    )
  }

  mainWindow.on('closed', () => { mainWindow = null })
}

// ─────────────────────── App lifecycle ───────────────────────────────────────

if (!squirrelEvent) app.whenReady().then(async () => {
  try {
    await startSidecar()
  } catch (err) {
    console.error('Failed to start sidecar:', err)
    app.quit()
    return
  }
  setupIPC()
  buildMenu()
  await createWindow()

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0) await createWindow()
  })
})

app.on('window-all-closed', () => {
  stopSidecar()
  if (process.platform !== 'darwin') app.quit()
})

app.on('will-quit', stopSidecar)
