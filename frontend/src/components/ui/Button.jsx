export function Button({ children, onClick, variant = 'default', size = 'sm',
  disabled = false, className = '', title }) {
  const base = 'inline-flex items-center gap-1.5 font-medium rounded transition-all duration-150 cursor-pointer select-none'
  const sizes = { xs: 'px-2 py-0.5 text-[11px]', sm: 'px-3 py-1 text-xs', md: 'px-4 py-1.5 text-sm' }
  const variants = {
    default: 'bg-cyber-tertiary border border-cyber-border text-cyber-text hover:border-cyber-cyan hover:text-cyber-cyan hover:shadow-[0_0_8px_#00d4ff30]',
    primary: 'bg-cyber-cyan/10 border border-cyber-cyan/50 text-cyber-cyan hover:bg-cyber-cyan/20 hover:shadow-[0_0_8px_#00d4ff40]',
    danger:  'bg-cyber-danger/10 border border-cyber-danger/50 text-cyber-danger hover:bg-cyber-danger/20',
    ghost:   'border border-transparent text-cyber-muted hover:text-cyber-text hover:border-cyber-border',
  }
  const dis = 'opacity-40 cursor-not-allowed pointer-events-none'

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`${base} ${sizes[size]} ${variants[variant]} ${disabled ? dis : ''} ${className}`}
    >
      {children}
    </button>
  )
}
