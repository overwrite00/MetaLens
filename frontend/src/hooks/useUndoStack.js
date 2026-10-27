import { useRef, useCallback, useState } from 'react'

const MAX_STACK = 50

export function useUndoStack() {
  const stack = useRef([])
  const [count, setCount] = useState(0)
  const [pending, setPending] = useState(0)
  const queue = useRef(Promise.resolve())
  const changed = () => setCount(stack.current.length)

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

  const undo = useCallback((restore) => runExclusive(async () => {
    const entry = stack.current[0]
    if (!entry) return null
    await restore(entry)
    stack.current = stack.current.filter(item => item !== entry)
    changed()
    return entry
  }), [runExclusive])

  return { push, canUndo, undo, runExclusive, busy: pending > 0 }
}
