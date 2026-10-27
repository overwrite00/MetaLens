import { createHash } from 'node:crypto'
import { expect, test } from 'vitest'
import { developmentCsp } from '../vite.config.js'

test('development CSP hashes inline scripts closed with browser-tolerated markup', () => {
  const html = "<meta http-equiv=\"Content-Security-Policy\" content=\"script-src 'self'\"><script>window.ready = true</script \t\n ignored=\"value\">"
  const result = developmentCsp.transformIndexHtml.handler(html)
  const hash = createHash('sha256').update('window.ready = true').digest('base64')

  expect(result).toContain(`script-src 'self' 'sha256-${hash}'`)
})

test('development CSP does not hash external scripts', () => {
  const html = "<meta http-equiv=\"Content-Security-Policy\" content=\"script-src 'self'\"><script src=\"/app.js\"></script >"

  expect(developmentCsp.transformIndexHtml.handler(html)).toContain("script-src 'self'")
  expect(developmentCsp.transformIndexHtml.handler(html)).not.toContain('sha256-')
})
