const { contextBridge, ipcRenderer } = require('electron')

const subscribe = (channel, callback) => {
  const listener = (_event, ...args) => callback(...args)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('electronAPI', {
  // Sidecar port
  getPort: () => ipcRenderer.invoke('get-port'),
  getSidecarConnection: () => ipcRenderer.invoke('get-sidecar-connection'),

  // Packaged app version (reflects beta suffix, unlike the sidecar's /health version)
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),

  // Repository link and update check (all network access happens in the main process)
  openRepo:            () => ipcRenderer.invoke('open-repo'),
  getUpdateSettings:   () => ipcRenderer.invoke('updates-get-settings'),
  setUpdateSettings:   (patch) => ipcRenderer.invoke('updates-set-settings', patch),
  checkForUpdates:     (options) => ipcRenderer.invoke('updates-check', options),
  openReleasePage:     () => ipcRenderer.invoke('updates-open-release'),
  installUpdate:       () => ipcRenderer.invoke('updates-install'),
  cancelUpdate:        () => ipcRenderer.invoke('updates-cancel'),
  onUpdateProgress:    (cb) => subscribe('update-progress', cb),

  // Native dialogs
  openFolderDialog: () => ipcRenderer.invoke('open-folder-dialog'),
  openFileDialog:   () => ipcRenderer.invoke('open-file-dialog'),

  // Path utilities (avoids exposing Node directly)
  pathSep:     () => ipcRenderer.invoke('path-sep'),
  pathJoin:    (...parts) => ipcRenderer.invoke('path-join', ...parts),
  pathDirname: (p) => ipcRenderer.invoke('path-dirname', p),
  listDrives:  () => ipcRenderer.invoke('list-drives'),
  homeDir:     () => ipcRenderer.invoke('home-dir'),

  // Menu events → React handlers
  onMenuOpenFolder: (cb) => subscribe('menu-open-folder', cb),
  onMenuUndo:       (cb) => subscribe('menu-undo', cb),
  onMenuAbout:      (cb) => subscribe('menu-about', cb),
  onMenuCheckUpdates: (cb) => subscribe('menu-check-updates', cb),

  // Cleanup
  removeAllListeners: (channel) => {
    if (['menu-open-folder', 'menu-undo', 'menu-about'].includes(channel)) ipcRenderer.removeAllListeners(channel)
  },
})
