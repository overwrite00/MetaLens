const test = require('node:test')
const assert = require('node:assert/strict')
const { buildMenuTemplate } = require('../menu')

const items = template => template.flatMap(menu => menu.submenu)
const find = (template, label) => items(template).find(item => item.label === label)

test('Edit > Undo shows Ctrl+Z but does not register it, so text fields keep native undo (D11)', () => {
  const sent = []
  const template = buildMenuTemplate((...args) => sent.push(args))
  const undo = find(template, 'Undo')
  assert.equal(undo.accelerator, 'CmdOrCtrl+Z')
  assert.equal(undo.registerAccelerator, false)
  assert.equal(undo.role, undefined)
  // No other item may grab Ctrl+Z (a role: 'undo' or a second accelerator).
  assert.deepEqual(items(template).filter(item => item.accelerator === 'CmdOrCtrl+Z' || item.role === 'undo'), [undo])

  undo.click(undo, null, { triggeredByAccelerator: false })
  undo.click(undo, null, { triggeredByAccelerator: true })
  undo.click(undo, null, undefined)
  assert.deepEqual(sent, [
    ['menu-undo', { keyboard: false }],
    ['menu-undo', { keyboard: true }],
    ['menu-undo', { keyboard: false }],
  ])
})

test('the rest of the menu is unchanged', () => {
  const sent = []
  const template = buildMenuTemplate((...args) => sent.push(args))
  assert.deepEqual(template.map(menu => menu.label), ['File', 'Edit', 'View', 'Help'])
  find(template, 'Open Folder…').click()
  find(template, 'Check for Updates…').click()
  find(template, 'About MetaLens').click()
  assert.deepEqual(sent, [['menu-open-folder'], ['menu-check-updates'], ['menu-about']])
  assert.equal(find(template, 'Open Folder…').accelerator, 'CmdOrCtrl+O')
  const roles = items(template).map(item => item.role).filter(Boolean)
  for (const role of ['quit', 'copy', 'paste', 'selectAll', 'reload', 'toggleDevTools', 'togglefullscreen']) {
    assert.ok(roles.includes(role), role)
  }
})

test('main.js builds the application menu from the shared template', () => {
  const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '..', 'main.js'), 'utf8')
  assert.match(source, /require\('\.\/menu'\)/)
  assert.match(source, /buildMenuTemplate\(/)
  assert.doesNotMatch(source, /accelerator:\s*'CmdOrCtrl\+Z'/)
})
