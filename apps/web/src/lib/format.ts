/** 展示格式化：接口时间为 UTC ISO，统一转 CST（R7）。 */

const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const dayFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  month: '2-digit',
  day: '2-digit',
})

export function formatCst(value: string | number | null | undefined): string {
  if (value == null) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : timeFormatter.format(date)
}

export function formatDay(value: string | null | undefined): string {
  if (value == null) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : dayFormatter.format(date)
}

/** 红涨绿跌（中式）：direction 1 → up（红），-1 → down（绿），0 → flat（灰）。 */
export function directionClass(direction: number): 'up' | 'down' | 'flat' {
  if (direction > 0) return 'up'
  if (direction < 0) return 'down'
  return 'flat'
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function signed(value: number | null | undefined, digits = 2): string {
  if (value == null) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`
}
