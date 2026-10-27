function isTrustedFrame(event, window, trustedURL) {
  return Boolean(window && !window.isDestroyed() &&
    event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame &&
    event.senderFrame.url.split('#')[0] === trustedURL)
}

function secureWindow(window) {
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', event => event.preventDefault())
  window.webContents.on('will-redirect', event => event.preventDefault())
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  window.webContents.session.setPermissionCheckHandler(() => false)
}

module.exports = { isTrustedFrame, secureWindow }
