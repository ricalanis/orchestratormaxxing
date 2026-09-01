const { test, expect } = require('@playwright/test');

const fixture = {
  year: 2026,
  objectives: [
    { id: 'economic', title: 'Económico', area: 'Trabajo, motivación y ahorro', progress: 45,
      status: 'at_risk', current_note: 'Ingreso es el cuello de botella',
      key_results: [{ id: 'billing', title: 'Elevar facturación', target: '$150k MXN/mes' }], checkin_count: 2 },
    { id: 'motivation', title: 'Motivación', area: 'Investigación, dirección, ejecución y comunicación',
      progress: null, status: 'active', current_note: 'Empresa activa; los otros tracks siguen pasivos',
      key_results: [{ id: 'phd', title: 'Avanzar admisión a posgrado', target: 'Programa de posgrado o equivalente' }], checkin_count: 0 },
    { id: 'round_man', title: 'Desarrollo integral', area: 'Crecimiento intelectual, emocional y artístico',
      progress: 80, status: 'active', current_note: 'Arte activo pendiente',
      key_results: [{ id: 'reading', title: 'Lectura consistente', target: 'Hábito sostenido' }], checkin_count: 1 },
    { id: 'ordered_life', title: 'Vida ordenada', area: 'Salud y relaciones', progress: 80,
      status: 'active', current_note: 'Retomando dieta y sueño',
      key_results: [{ id: 'body', title: 'Reducir peso', target: '85 kg' }], checkin_count: 1 },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route('**/api/personal/okrs*', async route => {
    if (route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      for (const change of body.objectives || []) {
        const objective = fixture.objectives.find(o => o.id === change.id);
        Object.assign(objective, change, { checkin_count: objective.checkin_count + 1 });
      }
      return route.fulfill({ json: fixture });
    }
    return route.fulfill({ json: fixture });
  });
});

test('OKRs follows Daily in Personal and lists four 2026 OKRs', async ({ page }) => {
  await page.goto('/');
  await page.click('#ws-personal');
  await expect(page.locator('#content-daily')).toBeVisible();
  await page.locator('#workspace-subnav button', { hasText: 'OKRs' }).click();
  await expect(page.locator('#content-okrs')).toBeVisible();
  await expect(page.locator('#workspace-subnav button').nth(1)).toContainText('OKRs');
  await expect(page.getByTestId('okrs-okr')).toHaveCount(4);
  await expect(page.getByTestId('okrs-okr').first()).toContainText('Económico');
  await expect(page.getByText('2026', { exact: true })).toBeVisible();
});

test('deep link works and save button persists the edited progress', async ({ page }) => {
  await page.goto('/?tab=okrs');
  const card = page.locator('[data-okr-id="economic"]');
  await expect(card).toBeVisible();
  await card.getByRole('button', { name: 'Actualizar' }).click();
  await card.locator('input[type="range"]').fill('52');
  await card.locator('textarea').fill('Subiendo ingreso');
  await card.getByRole('button', { name: 'Guardar avance' }).click();
  await expect(card).toContainText('52%');
  await expect(card).toContainText('Subiendo ingreso');
  await expect(card.getByRole('button', { name: 'Guardado' })).toBeVisible();
});

test('cancel button closes editor without changing visible state', async ({ page }) => {
  await page.goto('/?tab=okrs');
  const card = page.locator('[data-okr-id="round_man"]');
  await card.getByRole('button', { name: 'Actualizar' }).click();
  await card.locator('input[type="range"]').fill('20');
  await card.getByRole('button', { name: 'Cancelar' }).click();
  await expect(card).toContainText('80%');
  await expect(card.locator('[data-testid="okrs-editor"]')).toBeHidden();
});
