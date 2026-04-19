/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        'cyber-bg':        '#0a0e17',
        'cyber-panel':     '#0f1520',
        'cyber-tertiary':  '#1a2234',
        'cyber-border':    '#1e3a5f',
        'cyber-cyan':      '#00d4ff',
        'cyber-blue':      '#0066ff',
        'cyber-purple':    '#7c3aed',
        'cyber-text':      '#e2e8f0',
        'cyber-muted':     '#64748b',
        'cyber-dim':       '#334155',
        'cyber-success':   '#00ff88',
        'cyber-warning':   '#ffaa00',
        'cyber-danger':    '#ff4444',
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"Fira Code"', 'Consolas', 'monospace'],
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
