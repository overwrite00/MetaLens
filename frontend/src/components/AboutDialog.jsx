import { useEffect, useState } from 'react'
import { X, Layers } from 'lucide-react'
import { metalens } from '../api/client'

export function AboutDialog({ onClose }) {
  const [version, setVersion] = useState('…')

  useEffect(() => {
    metalens.health().then(d => setVersion(d.version)).catch(() => setVersion('?'))
  }, [])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}>
      <div className="relative bg-cyber-panel border border-cyber-cyan/30 rounded-lg p-8 w-80
        shadow-[0_0_40px_#00d4ff20] text-center"
        onClick={e => e.stopPropagation()}>

        <button onClick={onClose}
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
        </div>
      </div>
    </div>
  )
}
