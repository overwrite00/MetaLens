import { useEffect, useState } from 'react'
import { X, Layers, ExternalLink, RefreshCw, Download, Loader2 } from 'lucide-react'
import { Button } from './ui/Button'

const REPO_URL = 'https://github.com/overwrite00/MetaLens'

function UpdateSection({ updates }) {
  const { status, update, error, progress, settings } = updates
  const busy = status === 'checking' || status === 'downloading'
  const percent = progress?.total ? Math.floor(progress.received / progress.total * 100) : null

  return (
    <div className="border-t border-cyber-border mt-4 pt-4 text-left space-y-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-mono text-cyber-muted uppercase tracking-wider">Updates</span>
        <Button variant="default" size="xs" onClick={() => updates.check()} disabled={busy}>
          {status === 'checking' ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
          Check for updates
        </Button>
      </div>

      {status === 'current' && (
        <p className="text-[11px] text-cyber-success">You are running the latest version.</p>
      )}
      {error && <p role="alert" className="text-[11px] text-cyber-danger break-words">{error}</p>}

      {update && status !== 'checking' && status !== 'current' && (
        <div className="rounded border border-cyber-cyan/30 bg-cyber-cyan/5 p-3 space-y-2">
          <div className="text-xs text-cyber-text">
            {update.channel === 'beta' ? 'New beta available: ' : 'New version available: '}
            <span className="font-mono text-cyber-cyan">v{update.version}</span>
            {update.ignored && <span className="ml-2 text-[10px] text-cyber-muted">(ignored)</span>}
          </div>
          {update.notes && (
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words text-[10px]
              leading-relaxed text-cyber-muted font-mono bg-cyber-bg/60 rounded p-2">{update.notes}</pre>
          )}

          {status === 'downloading' ? (
            <div className="space-y-1.5">
              <div className="h-1.5 rounded bg-cyber-tertiary overflow-hidden">
                <div className="h-full bg-cyber-cyan transition-all" style={{ width: `${percent ?? 0}%` }} />
              </div>
              <div className="flex items-center justify-between text-[10px] font-mono text-cyber-muted">
                <span>{percent === null ? 'Starting download…' : `Downloading… ${percent}%`}</span>
                <Button variant="ghost" size="xs" onClick={updates.cancel}>Cancel</Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-wrap gap-2">
              {update.canInstall && (
                <Button variant="primary" size="xs" onClick={updates.install}>
                  <Download size={11} /> Download &amp; install
                </Button>
              )}
              <Button variant="default" size="xs" onClick={updates.openRelease}>
                <ExternalLink size={11} /> Release page
              </Button>
              <Button variant="ghost" size="xs" onClick={update.ignored ? updates.unignore : updates.ignore}>
                {update.ignored ? 'Stop ignoring' : 'Ignore this version'}
              </Button>
            </div>
          )}
        </div>
      )}

      <label className="flex items-start gap-2 text-[11px] text-cyber-muted cursor-pointer select-none">
        <input type="checkbox" className="mt-0.5 accent-cyan-400"
          checked={settings.autoCheckUpdates}
          onChange={e => updates.setAutoCheck(e.target.checked)} />
        <span>Check for updates automatically at startup
          <span className="block text-[10px] text-cyber-dim">
            Off by default. When on, MetaLens contacts GitHub once at launch; nothing else is sent.
          </span>
        </span>
      </label>
    </div>
  )
}

export function AboutDialog({ onClose, updates }) {
  const [version, setVersion] = useState('…')

  useEffect(() => {
    if (!window.electronAPI) {
      setVersion('?')
      return
    }
    window.electronAPI.getAppVersion().then(setVersion).catch(() => setVersion('?'))
  }, [])

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  function openRepo(e) {
    if (!window.electronAPI?.openRepo) return
    e.preventDefault()
    window.electronAPI.openRepo()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}>
      <div role="dialog" aria-label="About MetaLens"
        className="relative bg-cyber-panel border border-cyber-cyan/30 rounded-lg p-8 w-96 max-w-[95vw]
        max-h-[95vh] overflow-y-auto shadow-[0_0_40px_#00d4ff20] text-center"
        onClick={e => e.stopPropagation()}>

        <button onClick={onClose} aria-label="Close"
          className="absolute top-3 right-3 text-cyber-muted hover:text-cyber-text transition-colors">
          <X size={16} />
        </button>

        {/* Logo */}
        <div className="flex items-center justify-center w-16 h-16 rounded-full
          bg-cyber-cyan/10 border border-cyber-cyan/30 mx-auto mb-4
          shadow-[0_0_20px_#00d4ff30]">
          <Layers size={28} className="text-cyber-cyan" />
        </div>

        <h1 className="text-xl font-bold text-cyber-text tracking-wide mb-1">MetaLens</h1>
        <div className="font-mono text-cyber-cyan text-sm mb-3">v{version}</div>
        <p className="text-cyber-muted text-xs mb-4 leading-relaxed">
          Universal File Metadata Manager<br />
          Read, edit, delete &amp; compare metadata<br />
          across all major file formats.
        </p>

        <div className="border-t border-cyber-border pt-4 text-[11px] text-cyber-dim font-mono space-y-0.5">
          <div>© 2026 <span className="text-cyber-text">Graziano Mariella</span></div>
          <div className="text-cyber-cyan/70">MIT License</div>
          <div>
            <a href={REPO_URL} onClick={openRepo} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-cyber-cyan hover:underline">
              github.com/overwrite00/MetaLens <ExternalLink size={10} />
            </a>
          </div>
        </div>

        {updates?.supported && <UpdateSection updates={updates} />}
      </div>
    </div>
  )
}
