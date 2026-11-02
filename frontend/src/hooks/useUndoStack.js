import { useRef, useCallback, useState } from 'react'

const MAX_STACK = 50

/**
 * Why an undo error makes its entry useless (it is then dropped from the stack):
 * 'expired' (410: token unknown, used, evicted or sidecar restarted) or 'changed'
 * (409: the file changed since the save). `null` = transient error (403, 422, 500,
 * network): the entry stays and the user can retry.
 */
export function undoDiscardReason(error) {
  if (error?.code === 'undo_expired' || error?.status === 410) return 'expired'
  if (error?.code === 'concurrent_modification' || error?.status === 409) return 'changed'
  return null
}

/**
 * Session undo stack of saves, newest first. An entry is `{filePath, token, label}`:
 * the token comes from the sidecar's `/write` / `/delete` response.
 */
export function useUndoStack() {
  const stack = useRef([])
  const [count, setCount] = useState(0)
  const [pending, setPending] = useState(0)
  const queue = useRef(Promise.resolve())
  const changed = () => setCount(stack.current.length)
  const remove = entry => {
    stack.current = stack.current.filter(item => item !== entry)
    changed()
  }

  const push = useCallback((entry) => {
    stack.current = [entry, ...stack.current].slice(0, MAX_STACK)
    changed()
  }, [])

  const canUndo = useCallback(() => count > 0, [count])

  // Every mutation shares one queue, so an undo cannot race a pending save.
  const runExclusive = useCallback((operation) => {
    setPending(count => count + 1)
    const result = queue.current.then(operation)
    queue.current = result.catch(() => {})
    return result.finally(() => setPending(count => count - 1))
  }, [])

  // Undo the newest entry. On success it is removed; on 409/410 it is removed too
  // (it can never succeed) and the error is tagged with `undoDiscarded` and
  // `undoEntry`, without moving on to the next entry: the user decides whether to
  // undo the older one. Any other error keeps the entry for a retry.
  const undo = useCallback((restore) => runExclusive(async () => {
    const entry = stack.current[0]
    if (!entry) return null
    try {
      await restore(entry)
    } catch (error) {
      const reason = undoDiscardReason(error)
      if (reason) {
        remove(entry)
        if (error && typeof error === 'object') Object.assign(error, { undoDiscarded: reason, undoEntry: entry })
      }
      throw error
    }
    remove(entry)
    return entry
  }), [runExclusive])

  return { push, canUndo, undo, runExclusive, busy: pending > 0 }
}
