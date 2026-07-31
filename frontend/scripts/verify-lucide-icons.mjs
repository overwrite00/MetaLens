// Guards against lucide-react bumps silently renaming/removing icons we import.
// CI has no other signal for this: the build only fails if webpack/vite can't
// resolve the import, but named re-exports from lucide-react's barrel file
// still resolve to `undefined` at runtime instead of throwing at build time.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import * as lucide from 'lucide-react'

const SRC_DIR = join(import.meta.dirname, '..', 'src')
const IMPORT_RE = /import\s*\{([^}]+)\}\s*from\s*['"]lucide-react['"]/g

function walk(dir) {
  const entries = readdirSync(dir)
  const files = []
  for (const entry of entries) {
    const full = join(dir, entry)
    const stat = statSync(full)
    if (stat.isDirectory()) files.push(...walk(full))
    else if (/\.(jsx?|tsx?)$/.test(entry)) files.push(full)
  }
  return files
}

const usedIcons = new Set()
for (const file of walk(SRC_DIR)) {
  const content = readFileSync(file, 'utf-8')
  for (const match of content.matchAll(IMPORT_RE)) {
    for (const name of match[1].split(',')) {
      const trimmed = name.trim()
      if (trimmed) usedIcons.add(trimmed)
    }
  }
}

const missing = [...usedIcons].filter((name) => !(name in lucide))

if (missing.length > 0) {
  console.error(`Missing lucide-react icons (renamed or removed upstream): ${missing.join(', ')}`)
  process.exit(1)
}

console.log(`OK — all ${usedIcons.size} lucide-react icons used in src/ resolve correctly.`)
