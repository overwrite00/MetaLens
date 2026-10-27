const http = require('http')

function waitForSidecar(child, port, token, { timeout = 10000, interval = 200 } = {}) {
  return new Promise((resolve, reject) => {
    let timer, request, finished = false
    const deadline = Date.now() + timeout
    function finish(error) {
      if (finished) return
      finished = true
      clearTimeout(timer)
      request?.destroy()
      child.removeListener('error', onError)
      child.removeListener('exit', onExit)
      if (error) reject(error)
      else resolve(port)
    }
    const onError = error => finish(error)
    const onExit = () => finish(new Error('Sidecar exited before becoming ready'))
    child.once('error', onError)
    child.once('exit', onExit)
    function retry() {
      if (finished || timer) return
      if (Date.now() >= deadline) return finish(new Error('Sidecar startup timeout'))
      timer = setTimeout(poll, interval)
    }
    function poll() {
      timer = null
      if (finished) return
      request = http.get({ hostname: '127.0.0.1', port, path: '/health',
        headers: { Authorization: `Bearer ${token}` },
        timeout: Math.min(500, Math.max(1, deadline - Date.now())),
      }, response => {
        let body = ''
        response.setEncoding('utf8')
        response.on('data', chunk => {
          body += chunk
          if (body.length > 4096) request.destroy(new Error('Invalid sidecar health response'))
        })
        response.on('error', retry)
        response.on('end', () => {
          try {
            const health = JSON.parse(body)
            if (response.statusCode === 200 && health.status === 'ok' && health.app === 'MetaLens') {
              return finish()
            }
          } catch { /* The port may still belong to another service. */ }
          retry()
        })
      })
      request.on('timeout', () => request.destroy(new Error('Health request timeout')))
      request.on('error', retry)
    }
    poll()
  })
}

module.exports = { waitForSidecar }
