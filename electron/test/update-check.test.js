const test = require('node:test')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const {
  parseVersion, compareVersions, pickAsset, pickUpdate, checkForUpdates,
  downloadAsset, cleanupStaleDownloads, createSettingsStore,
} = require('../update-check')

const BASE = 'https://github.com/overwrite00/MetaLens'
const release = (tag, extra = {}) => ({
  tag_name: tag, draft: false, prerelease: tag.includes('-'), body: `notes ${tag}`,
  html_url: `${BASE}/releases/tag/${tag}`, published_at: '2026-10-30T10:00:00Z', assets: [], ...extra,
})
const winAsset = (tag, overrides = {}) => ({
  name: `MetaLens-${tag.slice(1)}-win-x64.exe`,
  browser_download_url: `${BASE}/releases/download/${tag}/MetaLens-${tag.slice(1)}-win-x64.exe`,
  size: 1000, digest: `sha256:${'a'.repeat(64)}`, ...overrides,
})
const tempDir = t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ml-test-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

test('versions follow semver precedence, including beta numbers', () => {
  const order = ['0.3.5', '0.3.6-beta.1', '0.3.6-beta.2', '0.3.6-beta.10', '0.3.6', '0.3.7-beta.1', '0.3.7', '0.10.0']
  for (let i = 0; i < order.length - 1; i++) {
    assert.equal(compareVersions(parseVersion(order[i]), parseVersion(order[i + 1])), -1, `${order[i]} < ${order[i + 1]}`)
    assert.equal(compareVersions(parseVersion(order[i + 1]), parseVersion(order[i])), 1)
  }
  assert.equal(compareVersions(parseVersion('v0.3.6'), parseVersion('0.3.6')), 0)
  assert.equal(parseVersion('latest'), null)
  assert.equal(parseVersion('1.2'), null)
})

test('a stable build only sees newer stable releases', () => {
  const releases = [release('v0.3.7-beta.2'), release('v0.3.6'), release('v0.3.5'), release('v0.3.6', { prerelease: true, tag_name: 'v0.3.8' })]
  assert.equal(pickUpdate(releases, '0.3.5', 'win32').version, '0.3.6')
  assert.equal(pickUpdate(releases, '0.3.5', 'win32').channel, 'stable')
  assert.equal(pickUpdate(releases, '0.3.6', 'win32'), null)
})

test('a beta build is told about newer betas and about the stable of its own version', () => {
  const current = '0.3.6-beta.2'
  assert.deepEqual(
    (u => [u.version, u.channel])(pickUpdate([release('v0.3.6-beta.3'), release('v0.3.6-beta.1')], current, 'win32')),
    ['0.3.6-beta.3', 'beta'])
  assert.deepEqual(
    (u => [u.version, u.channel])(pickUpdate([release('v0.3.6-beta.3'), release('v0.3.6')], current, 'win32')),
    ['0.3.6', 'stable'])
  assert.deepEqual(
    (u => [u.version, u.channel])(pickUpdate([release('v0.3.7-beta.1'), release('v0.3.6')], current, 'win32')),
    ['0.3.7-beta.1', 'beta'])
  assert.equal(pickUpdate([release('v0.3.6-beta.2'), release('v0.3.5')], current, 'win32'), null)
})

test('drafts, malformed tags and foreign release pages are ignored', () => {
  const releases = [
    release('v9.0.0', { draft: true }),
    release('nightly'),
    release('v8.0.0', { html_url: 'https://evil.example/releases/tag/v8.0.0' }),
    release('v0.4.0'),
  ]
  assert.equal(pickUpdate(releases, '0.3.6', 'win32').version, '0.4.0')
  assert.throws(() => pickUpdate(releases, 'garbage', 'win32'), /Unrecognized/)
  assert.equal(pickUpdate('not an array', '0.3.6', 'win32'), null)
})

test('release notes are capped and only the Windows installer with a checksum is selectable', () => {
  const notes = 'x'.repeat(50000)
  const update = pickUpdate([release('v0.4.0', { body: notes, assets: [winAsset('v0.4.0')] })], '0.3.6', 'win32')
  assert.equal(update.notes.length, 8000)
  assert.deepEqual(update.asset, {
    name: 'MetaLens-0.4.0-win-x64.exe', url: `${BASE}/releases/download/v0.4.0/MetaLens-0.4.0-win-x64.exe`,
    size: 1000, sha256: 'a'.repeat(64),
  })
  assert.equal(pickUpdate([release('v0.4.0', { assets: [winAsset('v0.4.0')] })], '0.3.6', 'linux').asset, null)

  const bad = [
    winAsset('v0.4.0', { digest: null }),
    winAsset('v0.4.0', { digest: 'sha256:abc' }),
    winAsset('v0.4.0', { browser_download_url: 'https://evil.example/MetaLens-0.4.0-win-x64.exe' }),
    winAsset('v0.4.0', { name: '../MetaLens-win-x64.exe' }),
    winAsset('v0.4.0', { size: 0 }),
    winAsset('v0.4.0', { size: 5 * 1024 ** 3 }),
    winAsset('v0.4.0', { name: 'metalens_0.4.0_amd64.deb' }),
  ]
  for (const asset of bad) assert.equal(pickAsset([asset], 'win32'), null, JSON.stringify(asset))
})

test('checkForUpdates reports HTTP and rate-limit failures clearly', async () => {
  const respond = (status, body, headers = {}) => async () => new Response(JSON.stringify(body), { status, headers })
  const run = fetchImpl => checkForUpdates({ fetch: fetchImpl, currentVersion: '0.3.6', platform: 'win32' })
  assert.equal((await run(respond(200, [release('v0.3.7')]))).version, '0.3.7')
  assert.equal(await run(respond(200, [])), null)
  await assert.rejects(run(respond(403, {}, { 'x-ratelimit-remaining': '0' })), /rate limit/)
  await assert.rejects(run(respond(500, {})), /HTTP 500/)
  await assert.rejects(run(respond(200, { message: 'nope' })), /Unexpected response/)
  let request
  await run(async (url, options) => { request = { url, options }; return new Response('[]') })
  assert.match(request.url, /^https:\/\/api\.github\.com\/repos\/overwrite00\/MetaLens\/releases/)
  assert.equal(request.options.headers['User-Agent'], 'MetaLens/0.3.6')
  assert.ok(request.options.signal)
})

const bodyOf = bytes => new Response(bytes).body
const assetFor = bytes => ({
  name: 'MetaLens-0.4.0-win-x64.exe', url: `${BASE}/releases/download/v0.4.0/x.exe`,
  size: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
})

test('download writes a verified file and reports progress', async t => {
  const baseDir = tempDir(t)
  const bytes = crypto.randomBytes(5000)
  const progress = []
  const { file } = await downloadAsset({
    fetch: async () => ({ ok: true, status: 200, body: bodyOf(bytes) }),
    asset: assetFor(bytes), baseDir, onProgress: p => progress.push(p),
  })
  assert.equal(path.dirname(path.dirname(file)), baseDir)
  assert.deepEqual(fs.readFileSync(file), bytes)
  assert.equal(progress.at(-1).received, bytes.length)
  assert.equal(progress.at(-1).total, bytes.length)
})

test('download rejects and removes files with a wrong checksum, wrong size or HTTP error', async t => {
  const baseDir = tempDir(t)
  const bytes = crypto.randomBytes(2000)
  const run = (asset, response) => downloadAsset({ fetch: async () => response, asset, baseDir })
  await assert.rejects(run({ ...assetFor(bytes), sha256: 'b'.repeat(64) }, { ok: true, body: bodyOf(bytes) }), /Checksum mismatch/)
  await assert.rejects(run({ ...assetFor(bytes), size: bytes.length + 1 }, { ok: true, body: bodyOf(bytes) }), /incomplete/)
  await assert.rejects(run({ ...assetFor(bytes), size: bytes.length - 1 }, { ok: true, body: bodyOf(bytes) }), /larger than expected/)
  await assert.rejects(run(assetFor(bytes), { ok: false, status: 404, body: null }), /HTTP 404/)
  assert.deepEqual(fs.readdirSync(baseDir), [])
})

test('download can be cancelled and leaves nothing behind', async t => {
  const baseDir = tempDir(t)
  const controller = new AbortController()
  const bytes = crypto.randomBytes(100)
  const body = new ReadableStream({
    start(stream) { stream.enqueue(bytes.subarray(0, 50)); controller.abort() },
  })
  await assert.rejects(downloadAsset({
    fetch: async () => ({ ok: true, body }), asset: assetFor(bytes), baseDir, signal: controller.signal,
  }))
  assert.deepEqual(fs.readdirSync(baseDir), [])
})

test('stale update downloads are removed, recent ones and unrelated folders are kept', async t => {
  const baseDir = tempDir(t)
  for (const name of ['metalens-update-old', 'metalens-update-new', 'other-folder']) fs.mkdirSync(path.join(baseDir, name))
  const old = new Date(Date.now() - 2 * 60 * 60 * 1000)
  fs.utimesSync(path.join(baseDir, 'metalens-update-old'), old, old)
  fs.utimesSync(path.join(baseDir, 'other-folder'), old, old)
  await cleanupStaleDownloads({ baseDir })
  assert.deepEqual(fs.readdirSync(baseDir).sort(), ['metalens-update-new', 'other-folder'])
})

test('settings default to off, persist, and reject unknown or invalid values', t => {
  const file = path.join(tempDir(t), 'sub', 'settings.json')
  const store = createSettingsStore(file)
  assert.deepEqual(store.get(), { autoCheckUpdates: false, ignoredVersion: null })
  assert.deepEqual(store.update({ autoCheckUpdates: true, ignoredVersion: '0.3.7' }), { autoCheckUpdates: true, ignoredVersion: '0.3.7' })
  assert.deepEqual(createSettingsStore(file).get(), { autoCheckUpdates: true, ignoredVersion: '0.3.7' })
  assert.throws(() => store.update({ autoCheckUpdates: 'yes' }), /Invalid value/)
  assert.throws(() => store.update({ ignoredVersion: 'latest' }), /Invalid value/)
  assert.throws(() => store.update({ other: 1 }), /Unknown setting/)
  assert.deepEqual(store.update({ ignoredVersion: null }), { autoCheckUpdates: true, ignoredVersion: null })
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ['settings.json'])
})

test('corrupt or tampered settings fall back to safe defaults', t => {
  const file = path.join(tempDir(t), 'settings.json')
  fs.writeFileSync(file, '{ not json')
  assert.deepEqual(createSettingsStore(file).get(), { autoCheckUpdates: false, ignoredVersion: null })
  fs.writeFileSync(file, JSON.stringify({ autoCheckUpdates: 'true', ignoredVersion: 5 }))
  assert.deepEqual(createSettingsStore(file).get(), { autoCheckUpdates: false, ignoredVersion: null })
})
