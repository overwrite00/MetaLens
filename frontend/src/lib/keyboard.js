// Ctrl+Z / Cmd+Z handling (plan D11): inside a text field it is the browser's own
// text undo; elsewhere it undoes the last save to a file.

const NON_TEXT_INPUTS = new Set(['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'])

/** True when `element` edits text natively (input, textarea, contenteditable). */
export function isTextEditable(element) {
  if (!element || element.nodeType !== 1) return false
  const tag = element.tagName
  if (tag === 'TEXTAREA') return !element.readOnly && !element.disabled
  if (tag === 'INPUT') {
    return !NON_TEXT_INPUTS.has(String(element.type || 'text').toLowerCase()) && !element.readOnly && !element.disabled
  }
  if (element.isContentEditable) return true
  // jsdom does not compute isContentEditable: honour the attribute up the tree.
  const host = element.closest?.('[contenteditable]')
  return Boolean(host) && String(host.getAttribute('contenteditable')).toLowerCase() !== 'false'
}

/** Ctrl+Z (Cmd+Z on macOS) without Shift/Alt: Ctrl+Shift+Z is redo, never undo. */
export function isUndoShortcut(event) {
  return (event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey
    && String(event.key).toLowerCase() === 'z'
}
