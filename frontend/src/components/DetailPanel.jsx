import { useState, useEffect, useCallback } from 'react'
import { Eye, Pencil, GitCompare, AlertTriangle, ShieldCheck, Copy, Check, Hash } from 'lucide-react'
import { Spinner } from './ui/Spinner'
import { MetadataTable } from './MetadataTable'
import { MetadataEditor } from './MetadataEditor'
import { DiffView } from './DiffView'
import { metalens } from '../api/client'

const HASH_ALGORITHMS = ['md5', 'sha1', 'sha256', 'sha512', 'blake2b']

function HashRow({ label, value }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="flex items-start gap-2 py-1.5 border-b border-cyber-border/40 last:border-0">
      <span className="w-16 flex-shrink-0 text-[10px] font-mono text-cyber-muted uppercase tracking-wider pt-0.5">
        {label}
      </span>
      <span className="flex-1 font-mono text-[10px] text-cyber-text break-all leading-relaxed">
        {value}
      </span>
      <button onClick={copy} title="Copy"
        className="flex-shrink-0 text-cyber-muted hover:text-cyber-cyan transition-colors mt-0.5">
        {copied ? <Check size={11} className="text-cyber-success" /> : <Copy size={11} />}
      </button>
    </div>
  )
}

function HashSection({ filePath }) {
  const [hashes, setHashes]   = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  // Reset when file changes
  useEffect(() => { setHashes(null); setError(null) }, [filePath])

  const compute = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const result = await metalens.hash(filePath, HASH_ALGORITHMS.join(','))
      setHashes(result.hashes)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [filePath])

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
            {loading ? 'Computing…' : 'Compute Hashes'}
          </button>
        )}
      </div>

      {error && (
        <div className="px-3 py-2 text-[10px] text-cyber-danger font-mono">{error}</div>
      )}

      {hashes && (
        <div className="px-3 py-1">
          {HASH_ALGORITHMS.map(alg => hashes[alg] && (
            <HashRow key={alg} label={alg} value={hashes[alg]} />
          ))}
          <button onClick={() => setHashes(null)}
            className="mt-1 mb-1 text-[10px] text-cyber-dim hover:text-cyber-muted transition-colors font-mono">
            clear
          </button>
        </div>
      )}
    </div>
  )
}

const TABS = [
  { id: 'view', label: 'Metadata', Icon: Eye },
  { id: 'edit', label: 'Edit',     Icon: Pencil },
  { id: 'diff', label: 'Diff',     Icon: GitCompare },
]

export function DetailPanel({ filePath, filePathB, onUndoPush }) {
  const [tab, setTab] = useState('view')
  const [record, setRecord] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!filePath) { setRecord(null); return }
    load(filePath)
  }, [filePath])

  // Auto-switch to diff tab when two files are selected
  useEffect(() => {
    if (filePathB) setTab('diff')
  }, [filePathB])

  async function load(path) {
    setLoading(true); setError(null)
    try {
      const data = await metalens.read(path)
      setRecord(data)
    } catch (e) {
      setError(e.message)
      setRecord(null)
    } finally {
      setLoading(false)
    }
  }

  const filename = filePath ? filePath.split(/[\\/]/).pop() : null

  return (
    <div className="flex flex-col h-full bg-cyber-panel border-l border-cyber-border">
      {/* File name */}
      {filename && (
        <div className="px-3 py-2 border-b border-cyber-border bg-cyber-bg/50">
          <div className="font-mono text-xs text-cyber-cyan truncate" title={filePath}>{filename}</div>
          {record && (
            <div className="text-[10px] text-cyber-dim font-mono mt-0.5">
              {record.handler_name} · {record.fields?.length ?? 0} fields
              {record.read_errors?.length > 0 &&
                <span className="text-cyber-warning ml-2">
                  <AlertTriangle size={9} className="inline mr-0.5" />
                  {record.read_errors.length} warning{record.read_errors.length > 1 ? 's' : ''}
                </span>
              }
            </div>
          )}
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b border-cyber-border bg-cyber-panel">
        {TABS.map(({ id, label, Icon }) => (
          <button key={id} onClick={() => setTab(id)}
            className={`flex items-center gap-1.5 px-3 py-2 text-xs transition-colors border-b-2
              ${tab === id
                ? 'border-cyber-cyan text-cyber-cyan bg-cyber-cyan/5'
                : 'border-transparent text-cyber-muted hover:text-cyber-text'}`}>
            <Icon size={12} /> {label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0">
        {loading && (
          <div className="flex items-center justify-center h-20 gap-2">
            <Spinner size={14} /><span className="text-xs text-cyber-muted">Reading…</span>
          </div>
        )}
        {error && (
          <div className="p-4 text-cyber-danger text-xs font-mono">{error}</div>
        )}
        {!loading && !error && !filePath && (
          <div className="flex items-center justify-center h-full text-cyber-dim text-xs">
            Select a file to inspect
          </div>
        )}
        {!loading && !error && filePath && tab === 'view' && (
          <div className="flex flex-col h-full overflow-y-auto">
            <MetadataTable fields={record?.fields ?? []} />
            <HashSection filePath={filePath} />
          </div>
        )}
        {!loading && !error && filePath && tab === 'edit' && (
          <MetadataEditor
            record={record}
            filePath={filePath}
            onSaved={() => load(filePath)}
            onUndoPush={onUndoPush}
          />
        )}
        {tab === 'diff' && (
          <DiffView fileA={filePath} fileB={filePathB} />
        )}
      </div>
    </div>
  )
}
