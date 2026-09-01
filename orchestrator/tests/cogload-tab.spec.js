// Contract for the redesigned Carga (cogload) tab.
//
// The information architecture changed 2026-08-13 (Open Design pass): the tab
// used to open with instrument diagnostics and then render five separate
// refusals, so with zero labels it read as a wall of "bloqueado". It now opens
// with the operator's actual question, demotes instrument health to a one-line
// strip, and collapses the refusals into one honest section that says what is
// missing.
//
// What this file guards is NOT the layout — it is the four invariants that
// outrank any redesign. The load-bearing one is C2/C3: a day the collector
// could not measure must never be presentable as a calm day. That is the
// single failure this whole surface exists to prevent, and a redesign is
// exactly the kind of change that would quietly reintroduce it (a green
// "Captura activa" pill is the natural default for a status strip).
//
// The payloads are stubbed via page.route so the assertions are deterministic
// and NOTHING is written to the real ~/.local/share/cogload store.
//
// Run: `npx playwright test cogload-tab`.
const { test, expect } = require('@playwright/test');

const API = '**/api/personal/cogload';

const stub = (page, payload) =>
  page.route(API, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(payload),
    }),
  );

// A store that exists but whose collector is DOWN. The dangerous case: there
// are no numbers to show, and "no numbers" must not read as "quiet day".
const DEGRADED = {
  status: {
    available: true,
    ok: false,
    last_status: 'degraded:session-wayland-xrecord-partial',
    reason: 'light: no-sample-in-window',
    subsystems: {
      keys: { available: true, reason: '' },
      screen: { available: true, reason: '' },
      light: { available: false, reason: 'no-sample-in-window' },
    },
  },
  days: [],
  weeks: [],
  months: [],
  readiness: {
    capture: { count: 0, target: 6, met: false },
    calibration: { count: 0, target: 14, met: false },
    curve: { count: 0, target: 14, met: false, load_levels: 0, load_levels_target: 3 },
  },
  fleet: { devices: [] },
};

const openTab = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('cogload'));
  await page.waitForFunction(
    () => {
      const el = document.getElementById('cogload-strip');
      return el && !/Comprobando captura/.test(el.textContent);
    },
    null,
    { timeout: 10000 },
  );
};

test.describe('Carga tab — information architecture invariants', () => {
  // C1 — the tab renders without a runtime error. A TDZ or a null host would
  // blank the panel, and a blank panel is indistinguishable from a calm day.
  test('C1 renders with no console errors', async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
    await stub(page, DEGRADED);
    await openTab(page);
    expect(errors, `console errors on the Carga tab:\n${errors.join('\n')}`).toEqual([]);
  });

  // C2 — THE load-bearing assertion. Collector down must never render as the
  // healthy green state, and the reason must be on screen.
  test('C2 a degraded collector never renders as active capture', async ({ page }) => {
    await stub(page, DEGRADED);
    await openTab(page);
    const strip = page.locator('#cogload-strip');
    await expect(strip).not.toContainText('Captura activa');
    await expect(strip).toContainText('Captura degradada');
    await expect(strip).toContainText('no-sample-in-window');

    const capture = page.locator('#cogload-capture');
    await expect(capture.locator('div.rounded-lg').filter({ hasText: 'Teclas' })).toContainText('activo');
    await expect(capture.locator('div.rounded-lg').filter({ hasText: 'Pantalla' })).toContainText('activo');
    await expect(capture.locator('div.rounded-lg').filter({ hasText: 'Luz' })).toContainText('no disponible');
  });

  // C3 — a day with no row is "sin datos", explicitly, and is NOT dressed as
  // calm. Absence must be its own state, never zero and never quiet.
  test('C3 a day with no data says so and is not shown as calm', async ({ page }) => {
    await stub(page, DEGRADED);
    await openTab(page);
    const today = page.locator('#cogload-today');
    await expect(today).toContainText('Sin datos de hoy');
    await expect(today).toContainText('no significa un día tranquilo');
    // No zeroed metric grid standing in for real numbers.
    await expect(today.locator('.tabular-nums')).toHaveCount(0);
  });

  // C4 — refusals survive the redesign. They are the product's integrity; the
  // redesign was allowed to group them, not to delete them, and each must say
  // what it still needs.
  test('C4 refusals stay visible and state what is missing', async ({ page }) => {
    await stub(page, DEGRADED);
    await openTab(page);
    const compare = page.locator('#cogload-compare');
    await expect(compare).toContainText('Aún no puedo comparar');
    await expect(compare).toContainText('Requiere');
    // And no trend/forecast is drawn before its threshold.
    await expect(compare.locator('svg, canvas')).toHaveCount(0);
  });

  // C5 — the empty state must read as progress, and each threshold must say
  // what it buys. This is what turns a locked door into a map.
  test('C5 thresholds show progress and what they unlock', async ({ page }) => {
    await stub(page, DEGRADED);
    await openTab(page);
    const readiness = page.locator('#cogload-readiness');
    await expect(readiness).toContainText('Desbloquea');
    await expect(readiness).toContainText('0/14');
    // Real targets, read from the API — not the invented ones.
    await expect(readiness).toContainText('0/6');
  });

  // C6 — the check-in is the only actionable thing on the page, so it is the
  // only thing with a button, and it must route into the reflection where the
  // check-in actually lives.
  test('C6 a missing check-in offers the route to the reflection', async ({ page }) => {
    await stub(page, DEGRADED);
    await openTab(page);
    const checkin = page.locator('#cogload-checkin');
    await expect(checkin).toContainText('Sin registrar');
    await expect(checkin.locator('button')).toHaveCount(1);
  });
});
