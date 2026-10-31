const fs = require('fs')
const os = require('os')
const path = require('path')
const crypto = require('crypto')
const { Readable, Transform } = require('stream')
const { pipeline } = require('stream/promises')

const REPO = 'overwrite00/MetaLens'
const REPO_URL = `https://github.com/${REPO}`
const RELEASES_API = `https://api.github.com/repos/${REPO}/releases?per_page=30`
const RELEASE_PAGE_PREFIX = `${REPO_URL}/releases/tag/`
const ASSET_URL_PREFIX = `${REPO_URL}/releases/download/`

const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/
const SHA256_RE = /^sha256:([0-9a-f]{64})$/
const SAFE_NAME_RE = /^[A-Za-z0-9._-]+$/
const WINDOWS_ASSET_RE = /-win-x64\.exe$/i
const MAX_ASSET_BYTES = 1024 * 1024 * 1024
const MAX_NOTES_CHARS = 8000
const REQUEST_TIMEOUT_MS = 15000

function parseVersion(text) {
  const match = VERSION_RE.exec(String(text).trim())
  if (!match) return null
  const [, major, minor, patch, pre] = match
  return { major: +major, minor: +minor, patch: +patch, pre: pre ? pre.split('.') : null }
}

function comparePrerelease(a, b) {
  if (!a && !b) return 0
  if (!a) return 1                    // a stable release outranks any of its prereleases
  if (!b) return -1
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (a[i] === undefined) return -1
    if (b[i] === undefined) return 1
    const aNum = /^\d+$/.test(a[i]), bNum = /^\d+$/.test(b[i])
    if (aNum && bNum) { if (+a[i] !== +b[i]) return +a[i] < +b[i] ? -1 : 1 }
    else if (aNum) return -1
    else if (bNum) return 1
    else if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1
  }
  return 0
}

function compareVersions(a, b) {
  for (const key of ['major', 'minor', 'patch']) {
    if (a[key] !== b[key]) return a[key] < b[key] ? -1 : 1
  }
  return comparePrerelease(a.pre, b.pre)
}

function pickAsset(assets, platform) {
  if (platform !== 'win32' || !Array.isArray(assets)) return null
  for (const asset of assets) {
    const digest = SHA256_RE.exec(String(asset?.digest))
    if (typeof asset?.name === 'string' && WINDOWS_ASSET_RE.test(asset.name) &&
        SAFE_NAME_RE.test(asset.name) && digest &&
        typeof asset.browser_download_url === 'string' &&
        asset.browser_download_url.startsWith(ASSET_URL_PREFIX) &&
        Number.isSafeInteger(asset.size) && asset.size > 0 && asset.size <= MAX_ASSET_BYTES) {
      return { name: asset.name, url: asset.browser_download_url, size: asset.size, sha256: digest[1] }
    }
  }
  return null
}

// A stable build only ever compares against stable releases; a beta build also
// considers newer betas, and a stable of the same or later version outranks them.
function pickUpdate(releases, currentVersion, platform) {
  const current = parseVersion(currentVersion)
  if (!current) throw new Error(`Unrecognized app version "${currentVersion}"`)
  const onBeta = current.pre !== null
  let best = null
  for (const release of Array.isArray(releases) ? releases : []) {
    if (!release || release.draft || typeof release.tag_name !== 'string') continue
    const parsed = parseVersion(release.tag_name)
    if (!parsed) continue
    const isBeta = parsed.pre !== null || release.prerelease === true
    if (isBeta && !onBeta) continue
    if (typeof release.html_url !== 'string' || !release.html_url.startsWith(RELEASE_PAGE_PREFIX)) continue
    if (compareVersions(parsed, current) <= 0) continue
    if (best && compareVersions(parsed, best.parsed) <= 0) continue
    best = { parsed, release, isBeta }
  }
  if (!best) return null
  const { release, isBeta } = best
  const version = release.tag_name.replace(/^v/, '')
  return {
    version,
    channel: isBeta ? 'beta' : 'stable',
    url: release.html_url,
    publishedAt: typeof release.published_at === 'string' ? release.published_at : null,
    notes: typeof release.body === 'string' ? release.body.slice(0, MAX_NOTES_CHARS) : '',
    asset: pickAsset(release.assets, platform),
  }
}

async function fetchReleases(fetchImpl, userAgent) {
  const response = await fetchImpl(RELEASES_API, {
    headers: { Accept: 'application/vnd.github+json', 'User-Agent': userAgent, 'X-GitHub-Api-Version': '2022-11-28' },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!response.ok) {
    const limited = response.status === 429 ||
      (response.status === 403 && response.headers.get('x-ratelimit-remaining') === '0')
    throw new Error(limited
      ? 'GitHub rate limit reached, try again later'
      : `GitHub responded with HTTP ${response.status}`)
  }
  const releases = await response.json()
  if (!Array.isArray(releases)) throw new Error('Unexpected response from GitHub')
  return releases
}

async function checkForUpdates({ fetch: fetchImpl, currentVersion, platform = process.platform }) {
  const releases = await fetchReleases(fetchImpl, `MetaLens/${currentVersion}`)
  return pickUpdate(releases, currentVersion, platform)
}

async function downloadAsset({ fetch: fetchImpl, asset, signal, onProgress = () => {}, baseDir = os.tmpdir() }) {
  const dir = await fs.promises.mkdtemp(path.join(baseDir, 'metalens-update-'))
  const file = path.join(dir, asset.name)
  try {
    const response = await fetchImpl(asset.url, { signal })
    if (!response.ok || !response.body) throw new Error(`Download failed (HTTP ${response.status})`)
    const hash = crypto.createHash('sha256')
    let received = 0
    const meter = new Transform({
      transform(chunk, _encoding, done) {
        received += chunk.length
        if (received > asset.size) return done(new Error('Downloaded file is larger than expected'))
        hash.update(chunk)
        onProgress({ received, total: asset.size })
        done(null, chunk)
      },
    })
    await pipeline(Readable.fromWeb(response.body), meter, fs.createWriteStream(file, { mode: 0o600 }), { signal })
    if (received !== asset.size) throw new Error('Downloaded file is incomplete')
    if (hash.digest('hex') !== asset.sha256) throw new Error('Checksum mismatch: the download was discarded')
    return { file, dir }
  } catch (error) {
    await fs.promises.rm(dir, { recursive: true, force: true }).catch(() => {})
    throw error
  }
}

// Installers are left in the temp dir because they are still running when the app
// quits, so remove leftovers of earlier runs (old enough not to belong to a live one).
async function cleanupStaleDownloads({ baseDir = os.tmpdir(), maxAgeMs = 60 * 60 * 1000, now = Date.now() } = {}) {
  for (const entry of await fs.promises.readdir(baseDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.startsWith('metalens-update-')) continue
    const dir = path.join(baseDir, entry.name)
    try {
      const { mtimeMs } = await fs.promises.stat(dir)
      if (now - mtimeMs >= maxAgeMs) await fs.promises.rm(dir, { recursive: true, force: true })
    } catch { /* in use or already gone */ }
  }
}

function createSettingsStore(file) {
  const defaults = { autoCheckUpdates: false, ignoredVersion: null }
  const validators = {
    autoCheckUpdates: value => typeof value === 'boolean',
    ignoredVersion: value => value === null || (typeof value === 'string' && parseVersion(value) !== null),
  }
  let settings = { ...defaults }
  try {
    const stored = JSON.parse(fs.readFileSync(file, 'utf8'))
    for (const key of Object.keys(defaults)) {
      if (validators[key](stored?.[key])) settings[key] = stored[key]
    }
  } catch { /* first run or unreadable file: keep the defaults */ }

  return {
    get: () => ({ ...settings }),
    update(patch) {
      const next = { ...settings }
      for (const [key, value] of Object.entries(patch ?? {})) {
        if (!validators[key]) throw new Error(`Unknown setting "${key}"`)
        if (!validators[key](value)) throw new Error(`Invalid value for setting "${key}"`)
        next[key] = value
      }
      const temp = `${file}.tmp`
      fs.mkdirSync(path.dirname(file), { recursive: true })
      fs.writeFileSync(temp, JSON.stringify(next, null, 2))
      fs.renameSync(temp, file)
      settings = next
      return { ...settings }
    },
  }
}

module.exports = {
  REPO_URL, parseVersion, compareVersions, pickAsset, pickUpdate,
  checkForUpdates, downloadAsset, cleanupStaleDownloads, createSettingsStore,
}
