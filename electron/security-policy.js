function isTrustedFrame(event, window, trustedURL) {
  return Boolean(window && !window.isDestroyed() &&
    event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame &&
    event.senderFrame.url.split('#')[0] === trustedURL)
}

function secureWindow(window, trustedURL) {
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', event => event.preventDefault())
  window.webContents.on('will-redirect', event => event.preventDefault())
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  const mayWriteClipboard = (contents, permission, details) => Boolean(
    permission === 'clipboard-sanitized-write' &&
    details?.isMainFrame === true &&
    details.requestingUrl?.split('#')[0] === trustedURL &&
    isTrustedFrame({ sender: contents, senderFrame: contents?.mainFrame }, window, trustedURL)
  )
  window.webContents.session.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(mayWriteClipboard(contents, permission, details))
  })
  window.webContents.session.setPermissionCheckHandler((contents, permission, _origin, details) => {
    return mayWriteClipboard(contents, permission, details)
  })
}

module.exports = { isTrustedFrame, secureWindow }
