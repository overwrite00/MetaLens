let _port = null

async function getPort() {
  if (_port) return _port
  if (window.electronAPI) {
    _port = await window.electronAPI.getPort()
  } else {
    _port = 57321 // fallback for browser dev
  }
  return _port
}

async function api(method, path, body = null) {
  const port = await getPort()
  const url = `http://127.0.0.1:${port}${path}`
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  if (body !== null) opts.body = JSON.stringify(body)
  const res = await fetch(url, opts)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

export const metalens = {
  health:    ()                    => api('GET',  '/health'),
  list:      (path)                => api('GET',  `/list?path=${encodeURIComponent(path)}`),
  read:      (path)                => api('GET',  `/read?path=${encodeURIComponent(path)}`),
  write:     (path, fields)        => api('POST', '/write',  { path, fields }),
  delete:    (path, keys)          => api('POST', '/delete', { path, keys }),
  diff:      (a, b)                => api('GET',  `/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  hash:      (path, algorithms)    => api('GET',  `/hash?path=${encodeURIComponent(path)}&algorithms=${encodeURIComponent(algorithms)}`),
}
