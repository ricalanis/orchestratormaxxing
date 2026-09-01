const { test, expect } = require('@playwright/test');

test.describe('Projects > Plan', () => {
  test('Cartera sigue siendo la vista por defecto', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('#projects-load')).toBeVisible();
    await expect(page.locator('#projects-plan')).toBeHidden();
    await expect(page.getByTestId('projects-view-cartera')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('projects-view-plan')).toHaveAttribute('aria-pressed', 'false');
  });

  test('un tap en + cambia media hora futura sin recargar', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');

    const plan = await page.evaluate(() => fetch('/api/projects/plan').then(r => r.json()));
    const target = plan.projects.find(project => {
      const cell = project.cells[1];
      return cell && cell.editable && (cell.hours === null || cell.hours < 40);
    });
    expect(target).toBeTruthy();
    const week = target.cells[1].iso_week;
    const before = target.cells[1].hours ?? 0;

    await page.getByTestId('projects-view-plan').click();
    await expect(page.locator('#projects-plan')).toBeVisible();
    const row = page.getByTestId('plan-row').filter({ hasText: target.name }).first();
    await expect(row).toBeVisible();

    await page.evaluate(() => { window.__projectsPlanStayedLoaded = true; });
    await row.getByTestId('plan-plus').first().click();

    const hours = () => page.evaluate(async ({ projectId, isoWeek }) => {
      const data = await fetch('/api/projects/plan').then(r => r.json());
      const project = data.projects.find(item => item.id === projectId);
      return project.cells.find(cell => cell.iso_week === isoWeek).hours;
    }, { projectId: target.id, isoWeek: week });
    await expect.poll(hours, { timeout: 5000 }).toBe(before + 0.5);
    await expect(row.getByTestId('plan-cell').first()).toHaveText(`${before + 0.5}h`);
    expect(await page.evaluate(() => window.__projectsPlanStayedLoaded)).toBe(true);
  });
});

test('el configurador tiene línea propia y NO se mueve al cambiar semanas', async ({ page }) => {
  // El operador lo reportó: el control vivía DENTRO de la rejilla de semanas, así que
  // pedir una tercera semana lo empujaba a otro renglón — el control se movía
  // justo al usarlo y el segundo tap caía en otro sitio.
  await page.goto('/?tab=projects');
  await page.waitForLoadState('networkidle');
  await page.locator('[data-testid="projects-view-plan"]').click();
  await expect(page.locator('[data-testid="projects-plan-config"]')).toBeVisible();

  // Vive FUERA de la rejilla de semanas: si estuviera dentro, reflowaría con ella.
  await expect(page.locator('#projects-plan-head [data-testid="plan-weeks"]')).toHaveCount(0);

  const plus = page.locator('[data-testid="plan-weeks-plus"]');
  const antes = await page.locator('[data-testid="plan-weeks"]').boundingBox();
  const semanasAntes = await page.locator('#projects-plan-head > div').count();

  await plus.click();
  await expect.poll(async () => page.locator('#projects-plan-head > div').count(),
                    { timeout: 5000 }).toBe(semanasAntes + 1);

  // La caja del control queda EXACTAMENTE donde estaba, con una columna más.
  const despues = await page.locator('[data-testid="plan-weeks"]').boundingBox();
  expect(Math.round(despues.y)).toBe(Math.round(antes.y));
  expect(Math.round(despues.x)).toBe(Math.round(antes.x));

  // Devolver el horizonte a donde estaba.
  await page.locator('[data-testid="plan-weeks-minus"]').click();
  await expect.poll(async () => page.locator('#projects-plan-head > div').count(),
                    { timeout: 5000 }).toBe(semanasAntes);
});
