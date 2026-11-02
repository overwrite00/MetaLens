import { Badge } from './ui/Badge'
import { formatItems, isBinaryDescriptor, uniqueKeys, valueTitle } from '../lib/formatValue'

const SOURCE_VARIANTS = {
  exif: 'cyan', gps: 'cyan', iptc: 'cyan', xmp: 'cyan', png: 'cyan', gif: 'cyan',
  id3: 'purple', vorbis: 'purple', mp4: 'purple', asf: 'purple', ape: 'purple', audio: 'purple', flac: 'purple',
  pdf: 'warning', office: 'warning', ole: 'warning',
  filesystem: 'default', xattr: 'default',
  hachoir: 'default', video: 'purple', image: 'cyan',
}

export function MetadataTable({ fields = [] }) {
  if (!fields.length) {
    return (
      <div className="flex items-center justify-center h-24 text-cyber-dim text-xs">
        No metadata fields
      </div>
    )
  }

  // Group by source
  const groups = {}
  for (const f of fields) {
    const src = f.source || 'unknown'
    if (!groups[src]) groups[src] = []
    groups[src].push(f)
  }

  return (
    <div className="overflow-y-auto h-full">
      {Object.entries(groups).map(([src, groupFields]) => {
        const keys = uniqueKeys(groupFields, f => f.key)
        return (
          <div key={src} className="mb-2">
            <div className="sticky top-0 z-10 px-3 py-1 bg-cyber-tertiary/80 backdrop-blur-sm
              text-[10px] font-mono text-cyber-muted uppercase tracking-wider border-b border-cyber-border">
              <Badge variant={SOURCE_VARIANTS[src] || 'default'}>{src}</Badge>
              <span className="ml-2">{groupFields.length} field{groupFields.length !== 1 ? 's' : ''}</span>
            </div>
            {groupFields.map((f, i) => (
              <div key={keys[i]}
                className="flex items-start px-3 py-1.5 border-b border-cyber-border/30 hover:bg-cyber-tertiary/40 group">
                <div className="w-44 flex-shrink-0 text-[11px] text-cyber-muted font-mono truncate pr-2" title={f.key}>
                  {f.label || f.key}
                </div>
                <div className="flex-1 min-w-0 font-mono text-[11px] text-cyber-text break-all leading-relaxed">
                  <FieldValue field={f} />
                </div>
                <div className="ml-2 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                  {!f.editable && <Badge variant="default">ro</Badge>}
                </div>
              </div>
            ))}
          </div>
        )
      })}
    </div>
  )
}

const Empty = () => <span className="text-cyber-dim">—</span>

/** Readable value of a field: list items as chips, binaries as descriptors. */
export function FieldValue({ field }) {
  const v = field.value
  if (v === null || v === undefined || v === '' || (Array.isArray(v) && v.length === 0)) return <Empty />
  const title = valueTitle(field)
  if (typeof v === 'boolean') return <span className={v ? 'text-cyber-success' : 'text-cyber-danger'}>{String(v)}</span>
  const items = formatItems(field)
  if (Array.isArray(v)) {
    return (
      <span className="inline-flex flex-wrap gap-1" title={title}>
        {items.map((text, i) => (
          <span key={i} className={`px-1 rounded border border-cyber-border/60 bg-cyber-bg/40
            ${isBinaryDescriptor(v[i]) ? 'text-cyber-dim italic' : ''}`}>{text || <Empty />}</span>
        ))}
      </span>
    )
  }
  const text = items[0] ?? ''
  if (isBinaryDescriptor(v) || (typeof v === 'string' && v.startsWith('<binary '))) {
    return <span className="text-cyber-dim italic" title={title}>{text}</span>
  }
  if (field.key?.startsWith('gps:') && typeof v === 'string') return <span className="text-cyber-cyan">{text}</span>
  return <span title={title}>{text}</span>
}
