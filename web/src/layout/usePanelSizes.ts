import { useEffect, useState } from 'react'
import { DEFAULT_PANELS, PANEL_STORAGE_KEY, panelSizes, readPanelPreferences } from './panelSizes'
import type { PanelName } from './panelSizes'

export function usePanelSizes(sidebarOpen: boolean, comparisonEnabled: boolean) {
  const [preferred, setPreferred] = useState(() => {
    try {
      return readPanelPreferences(localStorage.getItem(PANEL_STORAGE_KEY))
    } catch {
      return { ...DEFAULT_PANELS }
    }
  })
  const [viewport, setViewport] = useState(window.innerWidth)
  useEffect(() => {
    const resize = () => setViewport(window.innerWidth)
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])
  useEffect(() => {
    try {
      localStorage.setItem(PANEL_STORAGE_KEY, JSON.stringify(preferred))
    } catch {
      /* 저장이 차단되어도 현재 창에서는 조절할 수 있다 */
    }
  }, [preferred])
  const sizes = panelSizes(viewport, sidebarOpen, comparisonEnabled, preferred)
  return {
    ...sizes,
    resize: (name: PanelName, width: number) => {
      const min = name === 'sidebar' ? sizes.sidebarMin : sizes.comparisonMin
      const max = name === 'sidebar' ? sizes.sidebarMax : sizes.comparisonMax
      setPreferred((previous) => ({ ...previous, [name]: Math.min(max, Math.max(min, width)) }))
    },
    reset: (name: PanelName) => setPreferred((previous) => ({ ...previous, [name]: null })),
  }
}
