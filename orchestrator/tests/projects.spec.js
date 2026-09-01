const { test, expect } = require('@playwright/test');

test.describe('Projects workspace + consultant time', () => {
  test('routes, captures manual time, and restores a running timer after refresh', async ({ page }) => {
    const marker = `PW consulting ${Date.now()}`;
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');

    // Nav 8→5 (spec §4): Projects is a sub-view of the Work workspace, so the
    // lit nav button is #ws-work and the sub-view bar carries "Proyectos".
    await expect(page.locator('#ws-work')).toHaveClass(/active/);
    await expect(page.locator('#workspace-subnav')).toContainText('Proyectos');
    await expect(page.locator('#content-projects')).toBeVisible();
    await expect(page.locator('#projects-select')).toHaveValue(/proj_/);
    const projectId = await page.locator('#projects-select').inputValue();
    await expect(page).toHaveURL(new RegExp(`project_id=${encodeURIComponent(projectId)}`));

    await page.locator('#project-manual-description').fill(marker);
    await page.locator('#project-manual-minutes').fill('25');
    await page.locator('#project-manual-submit').click();
    await expect(page.locator('#project-time-recent')).toContainText(marker);
    await expect(page.locator('#project-time-today')).toContainText('25m');

    await page.locator('#project-timer-description').fill(`${marker} timer`);
    await page.locator('#project-timer-start').click();
    await expect(page.locator('#project-timer-status')).toContainText('Running');
    await page.reload();
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#project-timer-status')).toContainText('Running');
    await expect(page.locator('#project-timer-stop')).toBeVisible();
    await page.locator('#project-timer-stop').click();
    await expect(page.locator('#project-timer-status')).toContainText('No timer running');

    // Test hygiene: remove both uniquely labelled rows through the real API.
    await page.evaluate(async ({ projectId, marker }) => {
      const data = await fetch(`/api/consulting-time?project_id=${encodeURIComponent(projectId)}`).then(r => r.json());
      for (const entry of data.entries.filter(e => (e.description || '').startsWith(marker))) {
        await fetch(`/api/consulting-time/${encodeURIComponent(entry.id)}`, { method: 'DELETE' });
      }
    }, { projectId, marker });
  });

  test('mobile layout keeps the project selector and primary timer action available', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#projects-select')).toBeVisible();
    await expect(page.locator('#projects-rail')).toBeHidden();
    await expect(page.locator('#claude-summary-widget')).toBeHidden();
    await expect(page.locator('#project-timer-start')).toBeVisible();
    await expect(page.locator('#project-timer-start')).toHaveAttribute('aria-label', /timer/i);
  });
});
