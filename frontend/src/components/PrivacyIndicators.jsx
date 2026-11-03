import { ShieldAlert } from 'lucide-react'
import { Badge } from './ui/Badge'

// What a field can reveal when the file is shared (classified by the sidecar: `field.sensitivity`).
export const SENSITIVITY = {
  location: { label: 'Location', variant: 'danger', hint: 'Reveals where the file was made' },
  identity: { label: 'Identity', variant: 'danger', hint: 'Reveals who made or owns the file' },
  device: { label: 'Device', variant: 'warning', hint: 'Reveals the device or unique identifiers' },
  thumbnail: { label: 'Thumbnail', variant: 'warning', hint: 'Contains an embedded copy of the content' },
  datetime: { label: 'Date', variant: 'default', hint: 'Reveals when the file was made or changed' },
  software: { label: 'Software', variant: 'default', hint: 'Reveals the program used' },
  text: { label: 'Text', variant: 'default', hint: 'Free text written by a person' },
}

const ORDER = ['location', 'identity', 'device', 'thumbnail', 'text', 'datetime', 'software']

export function SensitivityBadge({ field }) {
  const info = SENSITIVITY[field?.sensitivity]
  if (!info) return null
  return <Badge variant={info.variant} className="ml-1" title={info.hint}>{info.label}</Badge>
}

/** Counts of fields per sensitivity category, most serious first. */
export function summarizeSensitivity(fields = []) {
  const counts = {}
  for (const f of fields) if (SENSITIVITY[f.sensitivity]) counts[f.sensitivity] = (counts[f.sensitivity] || 0) + 1
  return ORDER.filter(c => counts[c]).map(c => ({ category: c, count: counts[c] }))
}

/** One line telling what sharing this file would reveal. */
export function PrivacySummary({ fields = [] }) {
  const summary = summarizeSensitivity(fields).filter(s => SENSITIVITY[s.category].variant !== 'default'
    || s.category === 'datetime')
  if (!summary.length) return null
  return (
    <div role="note" aria-label="Privacy summary"
      className="flex flex-wrap items-center gap-1.5 px-3 py-1.5 border-b border-cyber-border bg-cyber-tertiary/40 text-[10px] font-mono text-cyber-muted">
      <ShieldAlert size={11} className="text-cyber-warning" aria-hidden="true" />
      <span>Sharing this file reveals:</span>
      {summary.map(({ category, count }) => (
        <Badge key={category} variant={SENSITIVITY[category].variant} title={SENSITIVITY[category].hint}>
          {SENSITIVITY[category].label} · {count}
        </Badge>
      ))}
    </div>
  )
}
