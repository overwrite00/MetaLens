import { useRef, useCallback } from 'react'

const MAX_STACK = 50

export function useUndoStack() {
  const stack = useRef([])

  const push = useCallback((entry) => {
    stack.current = [entry, ...stack.current].slice(0, MAX_STACK)
  }, [])

  const pop = useCallback(() => {
    if (!stack.current.length) return null
    const [top, ...rest] = stack.current
    stack.current = rest
    return top
  }, [])

  const canUndo = () => stack.current.length > 0

  return { push, pop, canUndo }
}
