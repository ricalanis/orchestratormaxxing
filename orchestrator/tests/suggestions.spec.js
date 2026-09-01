// e2e del gate humano de la digestión (pestaña Hoy → Sugerencias).
//
// Lo que se pinea aquí es que la tarjeta traiga con qué JUZGAR. Una sugerencia
// sin su cita textual no se puede evaluar, solo obedecer o ignorar — y a ese
// paso el gate humano deja de serlo: se vuelve un botón que se aprieta. Por eso
// la evidencia es contrato de UI, no adorno.
//
// Corre contra la copia sembrada por serve_test_dashboard.py (cita sintética).
// Run: `npx playwright test suggestions`.
const { test, expect } = require('@playwright/test');

async function abrirSugerencias(page) {
  await page.goto('/?tab=suggestions');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#content-suggestions')).toBeVisible();
}

test.describe('Sugerencias', () => {
  test('cada tarjeta trae su cita textual, que es con lo que se juzga',
    async ({ page }) => {
      await abrirSugerencias(page);
      const fila = page.locator('[data-testid=sug-row]').first();
      await expect(fila).toBeVisible();
      await expect(fila.locator('[data-testid=sug-title]')).not.toBeEmpty();
      const cita = fila.locator('[data-testid=sug-quote]');
      await expect(cita).toBeVisible();
      await expect(cita).toContainText('te la mando');
    });

  test('aceptar y descartar están los dos a la vista', async ({ page }) => {
    await abrirSugerencias(page);
    const fila = page.locator('[data-testid=sug-row]').first();
    await expect(fila.locator('[data-testid=sug-accept]')).toBeVisible();
    await expect(fila.locator('[data-testid=sug-dismiss]')).toBeVisible();
  });

  test('el alta rápida vive en la misma pantalla', async ({ page }) => {
    // Revisar sugerencias dispara memoria: al ver una, el operador se acuerda de otra
    // cosa. Mandarlo a otra pestaña para capturarla es donde se pierde.
    await abrirSugerencias(page);
    await expect(page.locator('[data-testid=sug-quick-input]')).toBeVisible();
    await expect(page.locator('[data-testid=sug-quick-add]')).toBeVisible();
  });

  // El POST se intercepta a propósito: `/api/tasks` invoca al CLI `hermes`, que
  // escribe SIEMPRE en ~/.hermes/kanban.db y no respeta la copia de pruebas. Un
  // spec que lo dejara pasar le mete tareas de prueba al kanban real del operador
  // — pasó, se midió y se limpió. Lo que aquí se prueba es la lógica del
  // navegador, que es justo lo que cambió.
  async function altaRapidaFalsa(page, { planFalla = false } = {}) {
    await page.route('**/api/tasks', route =>
      route.request().method() === 'POST'
        ? route.fulfill({ status: 200, contentType: 'application/json',
                          body: JSON.stringify({ id: 't_e2e_fake' }) })
        : route.continue());
    const planes = [];
    await page.route('**/api/tasks/*/plan', route => {
      planes.push(JSON.parse(route.request().postData() || '{}'));
      return planFalla ? route.abort()
                       : route.fulfill({ status: 200, contentType: 'application/json',
                                         body: JSON.stringify({ status: 'ok' }) });
    });
    return planes;
  }

  test('lo que el operador escribe queda planeado para hoy, no en el cajón',
    async ({ page }) => {
      // Escribirla ya fue la decisión. Mandarla a un cajón colapsado le pide
      // decidir dos veces lo mismo, y la segunda vez no se entera: la tarea
      // existe pero no está donde la fue a buscar.
      await abrirSugerencias(page);
      const planes = await altaRapidaFalsa(page);
      await page.locator('[data-testid=sug-quick-input]').fill('comprar cable HDMI');
      await page.locator('[data-testid=sug-quick-add]').click();

      await expect(page.locator('#sug-quick-msg')).toContainText('en tu Hoy');
      expect(planes.length, 'se planeó exactamente una vez').toBe(1);
      const hoy = new Date().toLocaleDateString('en-CA');
      expect(planes[0].planned_for, 'para HOY, en hora local').toBe(hoy);
    });

  test('si no se pudo planear, lo dice en vez de dejarla invisible',
    async ({ page }) => {
      // Callar el fallo es peor que el fallo: la tarea existe, no aparece donde
      // la buscó, y no hay ninguna señal de por qué.
      await abrirSugerencias(page);
      await altaRapidaFalsa(page, { planFalla: true });
      await page.locator('[data-testid=sug-quick-input]').fill('otra cosa');
      await page.locator('[data-testid=sug-quick-add]').click();
      await expect(page.locator('#sug-quick-msg')).toContainText('quedó en Later');
    });

  test('descartar saca la tarjeta de la lista y deja constancia', async ({ page }) => {
    await abrirSugerencias(page);
    const antes = await page.locator('[data-testid=sug-row]').count();
    expect(antes).toBeGreaterThan(0);

    await page.locator('[data-testid=sug-dismiss]').first().click();
    await page.waitForResponse(r => r.url().includes('/dismiss'));
    // Queda la lápida, no un hueco: una tarjeta que desaparece sin rastro se
    // siente como un clic perdido y se vuelve a intentar.
    await expect(page.locator('[data-testid=sug-tombstone]').first()).toBeVisible();

    const abiertas = await page.request.get('/api/suggestions?status=open')
      .then(r => r.json());
    expect(abiertas.length).toBe(antes - 1);
  });

  test('una sugerencia sin cita no puede pasar por una que la tiene',
    async ({ page }) => {
      // Contrato de forma: si algún día la evidencia deja de llegar, la tarjeta
      // tiene que notarse rota, no verse igual de confiable.
      await abrirSugerencias(page);
      const filas = await page.locator('[data-testid=sug-row]').all();
      for (const f of filas) {
        const citas = await f.locator('[data-testid=sug-quote]').count();
        expect(citas, 'toda tarjeta abierta muestra su evidencia').toBe(1);
      }
    });
});
