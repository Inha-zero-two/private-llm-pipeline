import assert from 'node:assert/strict'
import { test } from 'node:test'
import { DEFAULT_PANELS, panelSizes, readPanelPreferences } from '../src/layout/panelSizes.ts'

test('wide saved panels still leave usable chat space after shrinking the window', () => {
  for (const viewport of [1101, 1280, 1440, 1920]) {
    const sizes = panelSizes(viewport, true, true, { sidebar: 420, comparison: 800 })
    assert(viewport - sizes.sidebar - sizes.comparison >= 360)
    assert(sizes.sidebar >= 180 && sizes.sidebar <= 420)
    assert(sizes.comparison >= 260 && sizes.comparison <= 800)
  }
  const expanded = panelSizes(1920, true, true, { sidebar: 420, comparison: 800 })
  assert.equal(expanded.sidebar, 420)
  assert.equal(expanded.comparison, 800)
})

test('stacked or disabled comparison does not constrain the sidebar width', () => {
  assert.equal(panelSizes(1000, true, true, { sidebar: 420, comparison: 800 }).sidebar, 420)
  assert.equal(panelSizes(1280, true, false, { sidebar: 420, comparison: 800 }).sidebar, 420)
  assert.equal(panelSizes(1280, true, true, DEFAULT_PANELS).sidebar, 196)
})

test('invalid saved widths fall back without breaking layout', () => {
  assert.deepEqual(readPanelPreferences('{broken'), DEFAULT_PANELS)
  assert.deepEqual(readPanelPreferences('{"sidebar":-100,"comparison":"large"}'), DEFAULT_PANELS)
  const widths = panelSizes(
    1280,
    true,
    true,
    readPanelPreferences('{"sidebar":10000,"comparison":10000}'),
  )
  assert(1280 - widths.sidebar - widths.comparison >= 360)
})
