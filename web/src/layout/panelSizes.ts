export type PanelPreferences = { sidebar: number | null; comparison: number | null }
export type PanelName = keyof PanelPreferences

export const DEFAULT_PANELS: PanelPreferences = { sidebar: null, comparison: null }
const MAIN_MIN = 360
export const PANEL_STORAGE_KEY = 'pipeline.panel-widths.v1'

export function readPanelPreferences(value: string | null): PanelPreferences {
  try {
    const parsed = JSON.parse(value ?? 'null') as Partial<PanelPreferences> | null
    const width = (name: PanelName) => {
      const result = parsed?.[name]
      return typeof result === 'number' && Number.isFinite(result) && result > 0 ? result : null
    }
    return { sidebar: width('sidebar'), comparison: width('comparison') }
  } catch {
    return { ...DEFAULT_PANELS }
  }
}

/** 경계가 맞닿아도 가운데 채팅 공간을 남긴다. 작은 화면의 비교 패널은 기존처럼 아래로 배치한다. */
export function panelSizes(
  viewport: number,
  open: boolean,
  comparisonEnabled: boolean,
  preferred: PanelPreferences,
) {
  const sideBySide = viewport > 1100 && comparisonEnabled
  const comparisonMin = 260
  const sidebarMin = 180
  const wantedComparison = Math.min(
    800,
    Math.max(comparisonMin, preferred.comparison ?? Math.min(360, Math.max(280, viewport * 0.27))),
  )
  const sidebarMax = Math.max(
    sidebarMin,
    Math.min(
      420,
      viewport <= 768 ? viewport - 48 : viewport - MAIN_MIN - (sideBySide ? wantedComparison : 0),
    ),
  )
  const sidebar = Math.min(sidebarMax, Math.max(sidebarMin, preferred.sidebar ?? 196))
  const comparisonMax = Math.max(
    comparisonMin,
    Math.min(800, viewport - (open ? sidebar : 0) - MAIN_MIN),
  )
  const comparison = Math.min(comparisonMax, wantedComparison)
  return { sidebar, comparison, sidebarMin, sidebarMax, comparisonMin, comparisonMax }
}
