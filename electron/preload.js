const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  // Sidecar port
  getPort: () => ipcRenderer.invoke('get-port'),

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
  onMenuOpenFolder: (cb) => ipcRenderer.on('menu-open-folder', cb),
  onMenuUndo:       (cb) => ipcRenderer.on('menu-undo', cb),
  onMenuAbout:      (cb) => ipcRenderer.on('menu-about', cb),

  // Cleanup
  removeAllListeners: (channel) => ipcRenderer.removeAllListeners(channel),
})
