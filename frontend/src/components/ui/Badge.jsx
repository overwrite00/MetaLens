export function Badge({ children, variant = 'default', className = '' }) {
  const variants = {
    default:  'bg-cyber-tertiary text-cyber-muted border-cyber-border',
    cyan:     'bg-cyan-950/50 text-cyber-cyan border-cyber-cyan/30',
    purple:   'bg-purple-950/50 text-cyber-purple border-cyber-purple/30',
    success:  'bg-green-950/50 text-cyber-success border-cyber-success/30',
    warning:  'bg-yellow-950/50 text-cyber-warning border-cyber-warning/30',
    danger:   'bg-red-950/50 text-cyber-danger border-cyber-danger/30',
  }
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono
      border leading-none ${variants[variant] ?? variants.default} ${className}`}>
      {children}
    </span>
  )
}
