// Application menu template, kept free of Electron imports so it can be unit tested.
//
// Edit > Undo (plan D11): Ctrl+Z is displayed but NOT registered as a global
// accelerator (`registerAccelerator: false`, Windows/Linux), so the keystroke reaches
// the page: inside a text field it is the browser's native text undo, elsewhere the
// renderer turns it into the file undo. On macOS the accelerator is always handled by
// the menu: `keyboard: true` tells the renderer to undo the text field instead when
// one has focus. A mouse click on the item is always the file undo.

function buildMenuTemplate(send) {
  return [
    {
      label: 'File',
      submenu: [
        { label: 'Open Folder…', accelerator: 'CmdOrCtrl+O',
          click: () => send('menu-open-folder') },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { label: 'Undo', accelerator: 'CmdOrCtrl+Z', registerAccelerator: false,
          click: (_item, _window, event) =>
            send('menu-undo', { keyboard: Boolean(event?.triggeredByAccelerator) }) },
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
        { label: 'Check for Updates…',
          click: () => send('menu-check-updates') },
        { type: 'separator' },
        { label: 'About MetaLens',
          click: () => send('menu-about') },
      ],
    },
  ]
}

module.exports = { buildMenuTemplate }
