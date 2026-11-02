import { AlertTriangle, Info, Lock } from 'lucide-react'

// Notices that say nothing is wrong with the file: shown almost invisibly.
const QUIET_WARNINGS = [/^No format-specific metadata available\b/]
const isQuiet = text => QUIET_WARNINGS.some(re => re.test(text))

/**
 * Banners for a record: `read_errors` (blocking: format metadata cannot be modified),
 * `warnings` (non-blocking, collapsed) and, when `showReadOnly`, the format-level
 * read-only notice based on `format_supports_write` / `read_only_reason`.
 */
export function RecordNotices({ record, showReadOnly = false }) {
  if (!record) return null
  const errors = (record.read_errors ?? []).map(String)
  const warnings = (record.warnings ?? []).map(String)
  const quiet = warnings.filter(isQuiet)
  const loud = warnings.filter(w => !isQuiet(w))
  const readOnly = showReadOnly && record.format_supports_write === false
  if (!errors.length && !warnings.length && !readOnly) return null

  return (
    <div className="flex flex-col gap-1 px-3 py-1.5 border-b border-cyber-border">
      {errors.length > 0 && (
        <div role="alert" data-testid="read-errors"
          className="flex items-start gap-1.5 px-2 py-1 rounded border border-cyber-danger/40 bg-cyber-danger/5
            text-[10px] font-mono text-cyber-danger">
          <AlertTriangle size={11} className="flex-shrink-0 mt-0.5" />
          <div className="min-w-0">
            <div>Metadata could not be read completely: format metadata cannot be modified.</div>
            <ul className="list-disc ml-3 break-all">
              {errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          </div>
        </div>
      )}
      {readOnly && (
        <div data-testid="read-only-notice"
          className="flex items-start gap-1.5 px-2 py-1 rounded border border-cyber-warning/30 bg-cyber-warning/5
            text-[10px] font-mono text-cyber-warning">
          <Lock size={11} className="flex-shrink-0 mt-0.5" />
          <span className="min-w-0 break-words">
            Format metadata is read-only{record.read_only_reason ? `: ${record.read_only_reason}` : ''}.
            {' '}Filesystem fields such as the modification time stay editable.
          </span>
        </div>
      )}
      {loud.length > 0 && (
        <details role="status" data-testid="warnings" className="text-[10px] font-mono text-cyber-warning/80">
          <summary className="cursor-pointer select-none truncate">
            <Info size={10} className="inline mr-1 -mt-0.5" />
            {loud.length} notice{loud.length !== 1 ? 's' : ''}: {loud[0]}
          </summary>
          <ul className="list-disc ml-5 mt-0.5 text-cyber-muted break-all">
            {loud.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </details>
      )}
      {quiet.map((w, i) => (
        <div key={i} data-testid="quiet-notice" className="text-[10px] font-mono text-cyber-dim">{w}</div>
      ))}
    </div>
  )
}
