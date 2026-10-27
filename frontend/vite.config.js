import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { createHash } from 'node:crypto'

// Vite's React refresh preamble is inline in development. Authorize only its
// actual contents; production retains script-src 'self' without inline scripts.
const developmentCsp = {
  name: 'development-csp',
  apply: 'serve',
  transformIndexHtml: {
    order: 'post',
    handler(html) {
      const hashes = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)]
        .filter(([, attributes, code]) => !/\bsrc\s*=/.test(attributes) && code.trim())
        .map(([, , code]) => `'sha256-${createHash('sha256').update(code).digest('base64')}'`)
      return html.replace("script-src 'self'", `script-src 'self' ${hashes.join(' ')}`)
    },
  },
}

export default defineConfig({
  plugins: [tailwindcss(), react(), developmentCsp],
  base: './',
  server: { port: 5173 },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        entryFileNames: 'assets/index.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
})
