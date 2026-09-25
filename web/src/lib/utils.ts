/**
 * Shared utility functions.
 */

/** Merge class names, filtering out falsy values. */
export function cn(...inputs: (string | false | null | undefined)[]): string {
  return inputs.filter(Boolean).join(' ')
}

/**
 * Format an API timestamp in the browser's local timezone as "YYYY-MM-DD HH:mm".
 *
 * Accepts ISO strings with an explicit offset ("+08:00"/"Z" — converted to
 * local) or without one (interpreted as local wall-clock, e.g. naive
 * `datetime.now()` values from meta_scoring_runs). Never slice the raw string:
 * UTC wall-clock digits would display verbatim and look 8h off on UTC+8.
 */
export function formatApiDateTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '-'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** Format elapsed seconds since a timestamp as human-readable string. */
export function elapsedStr(startedAt: string | number | undefined): string {
  if (!startedAt) return '—'
  const start = typeof startedAt === 'number' ? startedAt : new Date(startedAt).getTime()
  const elapsed = Math.floor((Date.now() - start) / 1000)
  if (elapsed < 60) return `${elapsed}s`
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
  return `${Math.floor(elapsed / 3600)}h ${Math.floor((elapsed % 3600) / 60)}m`
}

/**
 * Detect exchange from A-share stock symbol.
 *
 * Rules:
 *  - 6xxxxx → SSE (沪市主板 600xxx, 科创板 688xxx/689xxx)
 *  - 0xxxxx/2xxxxx/3xxxxx → SZSE (深市主板 000xxx/002xxx, 创业板 300xxx/301xxx, B股 200xxx)
 *  - 8xxxxx/4xxxxx → BSE (北交所)
 */
export function detectExchange(symbol: string): 'SSE' | 'SZSE' | 'BSE' | 'UNKNOWN' {
  const s = symbol.replace(/\..*$/, '') // strip suffix like .SZ, .SH
  if (s.startsWith('6')) return 'SSE'
  if (s.startsWith('0') || s.startsWith('2') || s.startsWith('3')) return 'SZSE'
  if (s.startsWith('8') || s.startsWith('4')) return 'BSE'
  return 'UNKNOWN'
}

/** Return Chinese board label for an A-share symbol. */
export function getBoardLabel(symbol: string): string {
  const s = symbol.replace(/\..*$/, '')
  if (s.startsWith('688') || s.startsWith('689')) return '科创板'
  if (s.startsWith('300') || s.startsWith('301')) return '创业板'
  if (s.startsWith('8') || s.startsWith('4')) return '北交所'
  if (s.startsWith('6')) return '沪主板'
  if (s.startsWith('0') || s.startsWith('2')) return '深主板'
  return '未知'
}
