import { useEffect, useState, useCallback } from 'react'
import { GitCompare, Download, ShieldCheck, CheckCircle2, XCircle, Hash } from 'lucide-react'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'
import { metalens } from '../api/client'

const HASH_ALGORITHMS = ['md5', 'sha1', 'sha256', 'sha512', 'blake2b']

function DiffHashSection({ fileA, fileB }) {
  const [hashes, setHashes]   = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  useEffect(() => { setHashes(null); setError(null) }, [fileA, fileB])

  const compute = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const algs = HASH_ALGORITHMS.join(',')
      const [resA, resB] = await Promise.all([
        metalens.hash(fileA, algs),
        metalens.hash(fileB, algs),
      ])
      setHashes({ a: resA.hashes, b: resB.hashes })
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [fileA, fileB])

  return (
    <div className="border-t border-cyber-border mt-1">
      <div className="flex items-center justify-between px-3 py-2 bg-cyber-bg/30">
        <div className="flex items-center gap-1.5 text-[10px] font-mono text-cyber-muted uppercase tracking-wider">
          <ShieldCheck size={11} className="text-cyber-purple" />
          File Integrity
        </div>
        {!hashes && (
          <button onClick={compute} disabled={loading}
            className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded border border-cyber-border
              text-cyber-muted hover:text-cyber-cyan hover:border-cyber-cyan transition-colors disabled:opacity-50">
            {loading ? <Spinner size={10} /> : <Hash size={10} />}
            {loading ? 'Computing…' : 'Compare Hashes'}
          </button>
        )}
      </div>

      {error && (
        <div className="px-3 py-2 text-[10px] text-cyber-danger font-mono">{error}</div>
      )}

      {hashes && (
        <div className="px-3 py-1">
          {HASH_ALGORITHMS.map(alg => {
            const ha = hashes.a[alg]
            const hb = hashes.b[alg]
            if (!ha && !hb) return null
            const match = ha === hb
            return (
              <div key={alg} className="flex items-start gap-2 py-1.5 border-b border-cyber-border/40 last:border-0">
                <span className="w-16 flex-shrink-0 text-[10px] font-mono text-cyber-muted uppercase tracking-wider pt-0.5">
                  {alg}
                </span>
                <div className="flex-1 min-w-0 flex flex-col gap-0.5">
                  <span className="font-mono text-[9px] text-cyber-cyan break-all leading-relaxed">{ha}</span>
                  <span className="font-mono text-[9px] text-cyber-purple break-all leading-relaxed">{hb}</span>
                </div>
                <div className="flex-shrink-0 mt-0.5" title={match ? 'Match' : 'Mismatch'}>
                  {match
                    ? <CheckCircle2 size={13} className="text-cyber-success" />
                    : <XCircle      size={13} className="text-cyber-danger" />}
                </div>
              </div>
            )
          })}
          <button onClick={() => setHashes(null)}
            className="mt-1 mb-1 text-[10px] text-cyber-dim hover:text-cyber-muted transition-colors font-mono">
            clear
          </button>
        </div>
      )}
    </div>
  )
}

export function DiffView({ fileA, fileB }) {
  const [diff, setDiff] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!fileA || !fileB) return
    setLoading(true); setError(null)
    metalens.diff(fileA, fileB)
      .then(setDiff)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [fileA, fileB])

  if (!fileA || !fileB) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-2 text-cyber-dim">
        <GitCompare size={28} className="opacity-30" />
        <p className="text-xs">Select 2 files in the file list to compare</p>
      </div>
    )
  }
  if (loading) return <div className="flex items-center justify-center h-24 gap-2"><Spinner /> <span className="text-xs text-cyber-muted">Computing diff…</span></div>
  if (error) return <div className="p-4 text-cyber-danger text-xs">{error}</div>
  if (!diff) return null

  const nameA = fileA.split(/[\\/]/).pop()
  const nameB = fileB.split(/[\\/]/).pop()
  const totalChanges = diff.only_in_a.length + diff.only_in_b.length + diff.changed.length

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-3 py-2 border-b border-cyber-border bg-cyber-panel text-xs">
        <div className="flex-1 font-mono text-cyber-cyan truncate">{nameA}</div>
        <span className="text-cyber-muted">vs</span>
        <div className="flex-1 font-mono text-cyber-purple truncate text-right">{nameB}</div>
        <Button variant="ghost" size="xs" onClick={() => exportCsv(diff, nameA, nameB)}>
          <Download size={11} /> CSV
        </Button>
      </div>

      {/* Summary badges */}
      <div className="flex gap-3 px-3 py-2 text-[10px] font-mono border-b border-cyber-border">
        <span className="text-cyber-danger">{diff.only_in_a.length} only in A</span>
        <span className="text-cyber-success">{diff.only_in_b.length} only in B</span>
        <span className="text-cyber-warning">{diff.changed.length} changed</span>
        <span className="text-cyber-dim">{diff.identical.length} identical</span>
      </div>

      {/* Rows */}
      <div className="flex-1 overflow-y-auto">
        {totalChanges === 0 && (
          <div className="flex items-center justify-center h-16 text-cyber-success text-xs gap-1">
            ✓ Files have identical metadata
          </div>
        )}

        {diff.only_in_a.length > 0 && (
          <Section title="Only in A" color="text-cyber-danger">
            {diff.only_in_a.map(f => (
              <DiffRow key={f.key} label={f.label} valueA={String(f.value ?? '')} valueB="—" variant="a" />
            ))}
          </Section>
        )}

        {diff.only_in_b.length > 0 && (
          <Section title="Only in B" color="text-cyber-success">
            {diff.only_in_b.map(f => (
              <DiffRow key={f.key} label={f.label} valueA="—" valueB={String(f.value ?? '')} variant="b" />
            ))}
          </Section>
        )}

        {diff.changed.length > 0 && (
          <Section title="Changed" color="text-cyber-warning">
            {diff.changed.map(({ field_a, field_b }) => (
              <DiffRow key={field_a.key} label={field_a.label}
                valueA={String(field_a.value ?? '')} valueB={String(field_b.value ?? '')} variant="changed" />
            ))}
          </Section>
        )}

        <DiffHashSection fileA={fileA} fileB={fileB} />
      </div>
    </div>
  )
}

function Section({ title, color, children }) {
  return (
    <div className="mb-1">
      <div className={`sticky top-0 z-10 px-3 py-1 bg-cyber-tertiary/80 backdrop-blur-sm
        text-[10px] font-mono uppercase tracking-wider border-b border-cyber-border ${color}`}>
        {title}
      </div>
      {children}
    </div>
  )
}

function DiffRow({ label, valueA, valueB, variant }) {
  const rowBg = variant === 'a' ? 'bg-cyber-danger/5' : variant === 'b' ? 'bg-cyber-success/5' : 'bg-cyber-warning/5'
  const colA = variant === 'a' ? 'text-cyber-danger' : variant === 'changed' ? 'text-cyber-cyan' : 'text-cyber-dim'
  const colB = variant === 'b' ? 'text-cyber-success' : variant === 'changed' ? 'text-cyber-purple' : 'text-cyber-dim'
  return (
    <div className={`flex items-start px-3 py-1 border-b border-cyber-border/30 ${rowBg}`}>
      <div className="w-36 flex-shrink-0 text-[11px] text-cyber-muted font-mono truncate">{label}</div>
      <div className={`flex-1 min-w-0 font-mono text-[11px] break-all ${colA}`}>{valueA}</div>
      <div className="w-4 text-center text-cyber-dim text-[10px]">→</div>
      <div className={`flex-1 min-w-0 font-mono text-[11px] break-all ${colB}`}>{valueB}</div>
    </div>
  )
}

function exportCsv(diff, nameA, nameB) {
  const rows = [['Field', nameA, nameB, 'Status']]
  diff.only_in_a.forEach(f => rows.push([f.label, String(f.value ?? ''), '', 'only_in_a']))
  diff.only_in_b.forEach(f => rows.push([f.label, '', String(f.value ?? ''), 'only_in_b']))
  diff.changed.forEach(({ field_a, field_b }) =>
    rows.push([field_a.label, String(field_a.value ?? ''), String(field_b.value ?? ''), 'changed']))
  diff.identical.forEach(f => rows.push([f.label, String(f.value ?? ''), String(f.value ?? ''), 'identical']))

  const csv = rows.map(r => r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv' })
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
  a.download = `diff_${nameA}_vs_${nameB}.csv`; a.click()
}
