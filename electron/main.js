const { app, BrowserWindow, ipcMain, dialog, Menu } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const net = require('net')
const fs = require('fs')

let mainWindow = null
let sidecarProcess = null
let sidecarPort = null

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

  return new Promise((resolve, reject) => {
    sidecarProcess = spawn(sidecarBin, sidecarArgs, {
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    })

    sidecarProcess.stdout.on('data', (data) => { console.log('[sidecar]', data.toString().trim()) })
    sidecarProcess.stderr.on('data', (data) => { console.error('[sidecar]', data.toString().trim()) })

    sidecarProcess.on('error', reject)

    // Poll until the sidecar is accepting connections
    const deadline = Date.now() + 10000
    const poll = () => {
      const sock = net.connect(sidecarPort, '127.0.0.1')
      sock.on('connect', () => { sock.destroy(); resolve(sidecarPort) })
      sock.on('error', () => {
        if (Date.now() > deadline) return reject(new Error('Sidecar startup timeout'))
        setTimeout(poll, 200)
      })
    }
    setTimeout(poll, 300)
  })
}

function stopSidecar() {
  if (sidecarProcess) {
    sidecarProcess.kill()
    sidecarProcess = null
  }
}

// ─────────────────────── IPC handlers ────────────────────────────────────────

function setupIPC() {
  ipcMain.handle('get-port', () => sidecarPort)

  ipcMain.handle('open-folder-dialog', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openDirectory'],
    })
    return result.canceled ? null : result.filePaths[0]
  })

  ipcMain.handle('open-file-dialog', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile', 'multiSelections'],
    })
    return result.canceled ? [] : result.filePaths
  })

  ipcMain.handle('path-sep', () => path.sep)

  ipcMain.handle('path-join', (_, ...parts) => path.join(...parts))

  ipcMain.handle('path-dirname', (_, p) => path.dirname(p))

  ipcMain.handle('list-drives', () => {
    if (process.platform !== 'win32') return []
    // Return available Windows drive letters
    const drives = []
    for (let i = 65; i <= 90; i++) {
      const drive = String.fromCharCode(i) + ':\\'
      try { fs.accessSync(drive); drives.push(drive) } catch {}
    }
    return drives
  })

  ipcMain.handle('home-dir', () => require('os').homedir())
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
      sandbox: false,
    },
    icon: path.join(__dirname, '..', 'frontend', 'public', 'icon.png'),
  })

  const isDev = !app.isPackaged
  if (isDev) {
    await mainWindow.loadURL('http://localhost:5173')
    // mainWindow.webContents.openDevTools()
  } else {
    await mainWindow.loadFile(
      path.join(__dirname, '..', 'frontend', 'dist', 'index.html')
    )
  }

  mainWindow.on('closed', () => { mainWindow = null })
}

// ─────────────────────── App lifecycle ───────────────────────────────────────

app.whenReady().then(async () => {
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
