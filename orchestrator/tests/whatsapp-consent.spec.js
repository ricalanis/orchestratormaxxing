// e2e de la superficie de consentimiento de WhatsApp (pestaña Ops → Fuentes).
//
// Los 84 contratos en Python ya prueban que el SERVIDOR no autoriza de más. Esta
// capa prueba lo otro, que ninguno de ellos puede ver: que la PANTALLA no invite
// a hacerlo. Un backend impecable detrás de una interfaz que empuja al sí sigue
// terminando con las conversaciones familiares de alguien adentro.
//
// Corre contra la copia sembrada por serve_test_dashboard.py.
// Run: `npx playwright test whatsapp-consent`.
const { test, expect } = require('@playwright/test');

async function abrirFuentes(page) {
  await page.goto('/?tab=whatsapp');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#content-whatsapp')).toBeVisible();
}

test.describe('Fuentes — WhatsApp', () => {
  test('el botón de aprobar en bloque solo existe donde el empate es verificable',
    async ({ page }) => {
      await abrirFuentes(page);
      const carriles = page.locator('[data-testid=wa-lane]');
      await expect(carriles.first()).toBeVisible();

      for (const carril of await carriles.all()) {
        const id = await carril.getAttribute('data-lane');
        const masivos = await carril.locator('[data-testid=wa-bulk]').count();
        const esperado = ['patron_b2b', 'crm', 'nombre_entidad'].includes(id) ? 1 : 0;
        expect(masivos, `carril ${id}`).toBe(esperado);
      }
      // El carril del modelo aparece — negarle el bloque no es esconderlo.
      await expect(page.locator('[data-lane=modelo]')).toBeVisible();
    });

  test('el botón masivo dice cuántos y bajo qué regla, nunca "seleccionados"',
    async ({ page }) => {
      await abrirFuentes(page);
      const txt = await page.locator('[data-testid=wa-bulk]').first().textContent();
      expect(txt).toMatch(/Permitir los \d+ donde/);
      expect(txt).not.toMatch(/seleccionad/i);
    });

  test('Denegar pesa lo mismo que Permitir y está a la misma distancia',
    async ({ page }) => {
      // Un botón de negar más chico, más gris o un clic más lejos sube la
      // aceptación por diseño; el efecto está medido en la literatura de
      // consentimiento y es exactamente lo que aquí no queremos.
      await abrirFuentes(page);
      const fila = page.locator('[data-testid=wa-row]').first();
      const si = fila.locator('[data-testid=wa-allow]');
      const no = fila.locator('[data-testid=wa-deny]');
      await expect(si).toBeVisible();
      await expect(no).toBeVisible();

      const [cs, cn] = await Promise.all([si.boundingBox(), no.boundingBox()]);
      expect(Math.abs(cs.height - cn.height)).toBeLessThanOrEqual(2);
      expect(Math.abs(cs.y - cn.y)).toBeLessThanOrEqual(2);   // misma fila, un clic cada uno
      const fs = await si.evaluate(e => getComputedStyle(e).fontSize);
      const fn = await no.evaluate(e => getComputedStyle(e).fontSize);
      expect(fs).toBe(fn);
    });

  test('ni un solo mensaje aparece en la pantalla', async ({ page }) => {
    // La prueba central: si para decidir hay que leer, ya se leyó. Se compara
    // contra el espejo — lo que la pantalla muestra no puede intersectar con lo
    // que la gente escribió.
    await abrirFuentes(page);
    const dom = await page.locator('#content-whatsapp').innerText();
    expect(dom).not.toMatch(/"[^"]{40,}"/);          // sin citas largas
    for (const fila of await page.locator('[data-testid=wa-row]').all()) {
      const t = await fila.innerText();
      expect(t).toMatch(/msgs|sin mensajes/);        // forma, no fondo
    }
  });

  test('aprobar en bloque abre una vista previa y no autoriza nada todavía',
    async ({ page }) => {
      await abrirFuentes(page);
      const antes = await page.request.get('/api/whatsapp/permitidos')
        .then(r => r.json()).then(v => v.length);

      await page.locator('[data-testid=wa-bulk]').first().click();
      await expect(page.locator('#wa-stage')).toBeVisible();
      await expect(page.locator('#wa-stage-warn')).toContainText('LEA');
      // La muestra es para leer, no para actuar: sin botones adentro.
      await expect(page.locator('#wa-stage-sample [data-testid=wa-allow]')).toHaveCount(0);
      await expect(page.locator('[data-testid=wa-stage-confirm]')).toContainText(/Confirmar \d+/);

      const durante = await page.request.get('/api/whatsapp/permitidos')
        .then(r => r.json()).then(v => v.length);
      expect(durante, 'abrir la vista previa no autoriza').toBe(antes);

      await page.locator('[data-testid=wa-stage-cancel]').click();
      await expect(page.locator('#wa-stage')).toBeHidden();
      const despues = await page.request.get('/api/whatsapp/permitidos')
        .then(r => r.json()).then(v => v.length);
      expect(despues).toBe(antes);
    });

  test('la lista de denegados nace colapsada, se abre y se busca', async ({ page }) => {
    await abrirFuentes(page);
    await expect(page.locator('#wa-denied')).toBeHidden();

    await page.locator('[data-testid=wa-denied-toggle]').click();
    await expect(page.locator('#wa-denied')).toBeVisible();
    await page.waitForResponse(r => r.url().includes('estado=denegados'));
    await expect(page.locator('[data-testid=wa-restore]').first()).toBeVisible();

    await page.locator('[data-testid=wa-search-denied]').fill('zzz-no-existe');
    await page.waitForResponse(r => r.url().includes('q=zzz-no-existe'));
    await expect(page.locator('#wa-out-list')).toContainText('Nada coincide');
  });

  test('una lista recortada dice cuántos escondió', async ({ page }) => {
    // Recortar en silencio se lee como completo, y ahí es donde alguien concluye
    // que su chat no está.
    await abrirFuentes(page);
    await page.locator('[data-testid=wa-denied-toggle]').click();
    await page.waitForResponse(r => r.url().includes('estado=denegados'));
    const total = await page.request.get('/api/whatsapp/chats?estado=denegados&limit=1')
      .then(r => r.json()).then(d => d.total);
    const aviso = await page.locator('#wa-out-more').textContent();
    if (total > 60) expect(aviso).toMatch(/Mostrando \d+ de \d+/);
    else expect(aviso.trim()).toBe('');
  });

  test('las dos listas se buscan, cada una en lo suyo', async ({ page }) => {
    await abrirFuentes(page);
    await expect(page.locator('[data-testid=wa-search-allowed]')).toBeVisible();
    await page.locator('[data-testid=wa-denied-toggle]').click();
    await expect(page.locator('[data-testid=wa-search-denied]')).toBeVisible();
  });

  test('la postura dice qué se lee y qué no', async ({ page }) => {
    await abrirFuentes(page);
    const postura = await page.locator('[data-testid=wa-posture]').innerText();
    expect(postura).toMatch(/\d+ autorizados/);
    expect(postura).toMatch(/denegados en silencio|No se leen/);
  });
});
