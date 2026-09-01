// e2e for P1-1: primary dashboard workspaces, re-pinned to the consolidated
// 6-workspace nav (spec §4 collapsed 8→5; ADICIÓN 10 surfaced Personal back out
// of Ops — Hoy · Trabajo · Clientes · Agentes · Personal ·
// Ops). Multi-view workspaces (Trabajo/Clientes/Ops) expose a sub-view bar;
// single-view ones (Hoy, Agentes) hide it.
//
// The nav BUTTON IDS are the stable contract (`#ws-today`, `#ws-work`, …) —
// they are the workspace keys and are never translated. Only the visible labels
// went Spanish, so the label assertions below are the presentation half and the
// id assertions the structural half.
//
// Run: `npx playwright test workspaces`.
const { test, expect } = require('@playwright/test');

const load = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');   // window.onload → init lights the workspace nav
};

const wsButtons = (page) => page.locator('#workspace-nav button[onclick^="switchWorkspace"]');

test.describe('workspace nav (P1-1)', () => {
  test('shows 5 workspaces and defaults to Hoy', async ({ page }) => {
    await load(page);
    await expect(page.locator('#workspace-nav')).toBeVisible();
    await expect(wsButtons(page)).toHaveCount(6);
    for (const label of ['Hoy', 'Trabajo', 'Clientes', 'Agentes', 'Personal', 'Ops']) {
      await expect(page.locator('#workspace-nav')).toContainText(label);
    }
    await expect(page.locator('#ws-today')).toHaveClass(/active/);
    await expect(page.locator('#content-today')).toBeVisible();
  });

  test('Clientes groups Pipeline + Growth behind a sub-view bar', async ({ page }) => {
    await load(page);
    await page.click('#ws-clients');
    await expect(page.locator('#ws-clients')).toHaveClass(/active/);
    await expect(page.locator('#workspace-subnav')).toBeVisible();
    await expect(page.locator('#workspace-subnav button')).toHaveCount(2);
    await expect(page.locator('#content-crm')).toBeVisible();           // first sub

    // CRM Growth System: the second Clientes sub-view.
    await page.locator('#workspace-subnav button', { hasText: 'Growth' }).click();
    await expect(page.locator('#content-growth')).toBeVisible();
    await expect(page.locator('#content-crm')).toBeHidden();
    await expect(page.locator('#ws-clients')).toHaveClass(/active/);    // still Clientes
  });

  test('single-view Agentes hides the sub-view bar; Trabajo exposes seven subs', async ({ page }) => {
    await load(page);
    await page.click('#ws-agents');
    await expect(page.locator('#content-sessions')).toBeVisible();
    await expect(page.locator('#workspace-subnav')).toBeHidden();

    await page.click('#ws-work');
    await expect(page.locator('#workspace-subnav button')).toHaveCount(7);
    // Work absorbed Board · Ciclo · My Work · The Fleet · Archive · Proyectos · Roadmap.
    for (const label of ['Board', 'Ciclo', 'Proyectos', 'Roadmap']) {
      await expect(page.locator('#workspace-subnav')).toContainText(label);
    }
  });

  test('Ops owns the maintenance surfaces', async ({ page }) => {
    await load(page);
    await page.click('#ws-ops');
    await expect(page.locator('#workspace-subnav')).toBeVisible();
    for (const label of ['System health', 'Memory', 'Usage', 'Graph', 'Lakehouse']) {
      await expect(page.locator('#workspace-subnav')).toContainText(label);
    }
    // ADICIÓN 10 refinada (2026-08-03, operator): SYSTEM health is orchestrator
    // maintenance, so it lives here; what must NOT be here is the personal-care
    // cluster — its presence would be the regression.
    await expect(page.locator('#workspace-subnav')).not.toContainText('Plate');
    await expect(page.locator('#workspace-subnav')).not.toContainText('Supplements');
    // RE-PINNED: 'Coordinators' left this list. Spec §1 deletes it from the nav
    // — it is a derived keyword lens over agents, not a place — so its presence
    // here would be the regression, not its absence.
    await expect(page.locator('#workspace-subnav')).not.toContainText('Coordinators');
  });

  test('Personal surfaces the care cluster', async ({ page }) => {
    await load(page);
    await page.click('#ws-personal');
    await expect(page.locator('#workspace-subnav')).toBeVisible();
    for (const label of ['OKRs', 'Daily', 'Plate', 'Supplements', 'Reflexión']) {
      await expect(page.locator('#workspace-subnav')).toContainText(label);
    }
    await expect(page.locator('#workspace-subnav button').first()).toContainText('Daily');
    await expect(page.locator('#content-daily')).toBeVisible();
    // System health moved to Ops (2026-08-03) — a health entry HERE would be
    // the maintenance/care conflation coming back.
    await expect(page.locator('#workspace-subnav')).not.toContainText('health');
  });

  test('coordinators left the nav but is still reachable by deep link', async ({ page }) => {
    // "Removed from the nav" ≠ 404. The tab key stays routable so an existing
    // link keeps resolving; only the nav entry is gone.
    await page.goto('/?tab=coordinators');
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#content-coordinators')).toBeVisible();
    await expect(page.locator('#workspace-subnav')).not.toContainText('Coordinators');
  });
});
