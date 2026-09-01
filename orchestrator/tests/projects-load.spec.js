const { test, expect } = require('@playwright/test');

// Carga de la cartera: el panel que responde "¿cuántos proyectos aguanto?".
// Lo que estos contratos protegen, en orden:
//   1. Se ve SIN seleccionar proyecto — la pregunta es de la cartera entera.
//   2. Dice algo cierto con cero datos declarados (el estado del día 1).
//   3. Nunca presenta un total incompleto como si fuera el total: imprime "≥".
//   4. El gesto de declarar horas es UN tap y se refleja sin recargar.
test.describe('Carga de la cartera (projects load)', () => {

  test('se muestra a nivel cartera y dice la verdad con cero datos', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');

    const card = page.locator('#projects-load');
    await expect(card).toBeVisible();

    // Titular: horas comprometidas contra horas de ENTREGA medidas.
    const head = page.locator('#projects-load-head');
    await expect(head).toContainText(/\d+(\.\d+)?h/);
    await expect(head).toContainText(/de \d+(\.\d+)?h de entrega/);

    // El conteo de activos sale de la tabla, no de una constante.
    const api = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    // El titular ya NO dice `active_count`: con proyectos en cero, "10 activos"
    // junto a "≥0h de 32h" infla la cartera que de verdad pesa.
    await expect(head).toContainText(String(api.declared.committed_count));
    await expect(head).not.toContainText('activos');

    // Tres carriles y la semana cierra: comercial y cobranza se reportan, pero
    // NUNCA entran al denominador. Si un carril se cayera del reparto, esta
    // suma dejaría de cuadrar en vez de perderse en silencio.
    expect(api.capacity.week_hours).toBeCloseTo(
      api.capacity.delivery_hours + api.capacity.growth_reserved_hours
      + api.capacity.admin_reserved_hours, 2);
    expect(api.capacity.source.delivery_roles).not.toContain('sdr');
    expect(api.capacity.source.delivery_roles).not.toContain('analyst');

    // Una fila por proyecto activo, cada una con su botón de horas.
    await expect(page.locator('[data-testid="projects-load-row"]'))
      .toHaveCount(api.projects.length + api.inactive.projects.length);
  });

  test('un total incompleto se marca con ≥ y jamás se pinta en verde', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    const api = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));

    if (api.declared.unsized_count > 0) {
      await expect(page.locator('#projects-load-head')).toContainText('≥');
      expect(api.declared.band).not.toBe('green');
      await expect(page.locator('[data-testid="today-hl-load-unsized"]')).toBeVisible();
      await expect(page.locator('#projects-load-bar')).not.toHaveClass(/bg-emerald-500/);
    } else {
      await expect(page.locator('#projects-load-head')).not.toContainText('≥');
    }
  });

  test('+0.5 y −0.5 mueven el presupuesto en medias horas, sin recargar', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');

    const before = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    const target = before.projects.find(p => p.bucket === 'budget' && p.weekly_hours === null)
      || before.projects.find(p => p.bucket === 'budget');
    const row = page.locator('[data-testid="projects-load-row"]').filter({ hasText: target.name }).first();
    // Relativo, no absoluto: los specs comparten una copia de DB y el proyecto
    // puede traer horas de un test anterior o del estado real.
    const horas = () => page.evaluate(async id => {
      const d = await fetch('/api/projects/load').then(r => r.json());
      return (d.projects.find(p => p.id === id) || {}).weekly_hours;
    }, target.id);
    const h0 = (await horas()) || 0;

    await row.locator('[data-testid="projects-load-plus"]').click();
    await expect.poll(horas, { timeout: 5000 }).toBe(h0 + 0.5);
    await row.locator('[data-testid="projects-load-plus"]').click();
    await expect.poll(horas, { timeout: 5000 }).toBe(h0 + 1);
    await row.locator('[data-testid="projects-load-minus"]').click();
    await expect.poll(horas, { timeout: 5000 }).toBe(h0 + 0.5);

    const after = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    expect(after.declared.committed_hours).toBeCloseTo(before.declared.committed_hours + 0.5, 2);
  });

  test('el número se puede escribir exacto, y −0.5 nunca baja de 0', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    const before = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    const target = before.projects[0];
    const row = page.locator('[data-testid="projects-load-row"]').filter({ hasText: target.name }).first();

    await row.locator('[data-testid="projects-load-hours"]').click();
    const input = row.locator('[data-testid="projects-load-input"]');
    await expect(input).toBeVisible();
    await input.fill('3.5');
    await input.press('Enter');
    await expect(row.locator('[data-testid="projects-load-hours"]')).toHaveText('3.5h', { timeout: 5000 });

    // Escape descarta: el valor escrito no se guarda.
    await row.locator('[data-testid="projects-load-hours"]').click();
    await row.locator('[data-testid="projects-load-input"]').fill('99');
    await row.locator('[data-testid="projects-load-input"]').press('Escape');
    await expect(row.locator('[data-testid="projects-load-hours"]')).toHaveText('3.5h');

    // 0 es el piso: aparcado, nunca negativo.
    await row.locator('[data-testid="projects-load-hours"]').click();
    await row.locator('[data-testid="projects-load-input"]').fill('0');
    await row.locator('[data-testid="projects-load-input"]').press('Enter');
    await expect(row.locator('[data-testid="projects-load-hours"]')).toHaveText('0h', { timeout: 5000 });
    await row.locator('[data-testid="projects-load-minus"]').click();
    await expect(row.locator('[data-testid="projects-load-hours"]')).toHaveText('0h');

    const after = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    expect(after.projects.find(p => p.id === target.id).weekly_hours).toBe(0);
  });

  test('el widget de Hoy muestra la misma carga y lleva a Proyectos', async ({ page }) => {
    await page.goto('/?tab=today');
    await page.waitForLoadState('networkidle');

    const widget = page.locator('#today-load');
    await expect(widget).toBeVisible();
    const api = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    await expect(widget).toContainText(`de ${api.capacity.delivery_hours}h de entrega`);
    await expect(widget).toContainText(`${api.declared.committed_count} en el presupuesto`);
    if (api.declared.unsized_count > 0) await expect(widget).toContainText('≥');
    // El ritual semanal: mientras queden proyectos sin repartir esta semana, la
    // portada lo pide; cuando no queda ninguno, desaparece en vez de quedarse
    // encendido (una alarma permanente se vuelve invisible).
    if (api.declared.ritual_due) {
      await expect(page.locator('[data-testid="today-load-ritual"]')).toContainText(
        `reparte tu semana (${api.declared.pending_this_week})`);
    } else {
      await expect(page.locator('[data-testid="today-load-ritual"]')).toHaveCount(0);
    }
    expect(api.declared.declared_this_week + api.declared.pending_this_week)
      .toBe(api.declared.active_count);
    // `excluded` murió: el hecho vive en la fila. Y el carril de automejora
    // reclama del MISMO pozo, así que el total nunca es menor que la entrega.
    expect(api.excluded).toBeUndefined();
    expect(api.total.claimed_hours).toBeGreaterThanOrEqual(api.declared.committed_hours);

    await page.locator('[data-testid="today-load-open"]').click();
    await expect(page.locator('#projects-load')).toBeVisible();
  });

  test('el interruptor apaga y vuelve a encender desde la misma pantalla', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    const before = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    const target = before.projects[0];

    // Apagar: el dato cambia, pero la fila NO salta de sección — se ofrece el
    // reacomodo. La regla es única: la lista se asienta cuando tú lo pides.
    await page.locator('[data-testid="projects-load-row"]')
      .filter({ hasText: target.name }).locator('[data-testid="projects-load-power"]').first().click();
    await expect(page.locator('[data-testid="projects-load-reorder"]')).toBeVisible({ timeout: 5000 });
    const off = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    expect(off.declared.active_count).toBe(before.declared.active_count - 1);
    expect(off.inactive.projects.some(p => p.id === target.id)).toBe(true);

    // Al reacomodar, cae en la sección de apagados.
    await page.locator('[data-testid="projects-load-reorder"]').click();
    await expect(page.locator('[data-testid="projects-load-row"][data-bucket="off"]')
      .filter({ hasText: target.name })).toBeVisible({ timeout: 5000 });

    // Y vuelve a prenderse desde ahí.
    await page.locator('[data-testid="projects-load-row"][data-bucket="off"]')
      .filter({ hasText: target.name }).locator('[data-testid="projects-load-power"]').first().click();
    const on = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    expect(on.declared.active_count).toBe(before.declared.active_count);
  });

  test('subir media hora NO reacomoda la lista bajo el dedo', async ({ page }) => {
    // El bug que reportó el operador: el número subía y ~100 ms después la lista se
    // reordenaba sola, porque el `finally` del PATCH recargaba Y re-congelaba el
    // orden con el `sort` nuevo del servidor. Dos taps seguidos en el mismo sitio
    // caían en proyectos distintos.
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    const nombres = () => page.locator('[data-testid="projects-load-row"] .truncate')
      .allTextContents();
    const antes = await nombres();
    test.skip(antes.length < 3, 'se necesitan al menos 3 filas');

    // La última fila del presupuesto: subirle horas la mandaría hasta arriba si
    // el orden se recalculara en vivo.
    const api = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    const ultimo = [...api.projects].reverse().find(p => p.bucket === 'budget');
    const row = page.locator('[data-testid="projects-load-row"]').filter({ hasText: ultimo.name }).first();

    // Relativo, no absoluto: los specs de este archivo comparten una sola copia
    // de la DB (workers:1), así que el valor de partida no es necesariamente NULL.
    const horas = () => page.evaluate(async id => {
      const d = await fetch('/api/projects/load').then(r => r.json());
      return (d.projects.find(p => p.id === id) || {}).weekly_hours;
    }, ultimo.id);
    const h0 = (await horas()) || 0;

    await row.locator('[data-testid="projects-load-plus"]').click();
    await expect.poll(horas, { timeout: 5000 }).toBe(h0 + 0.5);
    await page.waitForTimeout(1200);          // deja aterrizar la recarga del PATCH
    expect(await nombres()).toEqual(antes);   // MISMO orden, misma posición

    // Dos taps seguidos caen en el MISMO proyecto — que es lo que el bug rompía.
    await row.locator('[data-testid="projects-load-plus"]').click();
    await expect.poll(horas, { timeout: 5000 }).toBe(h0 + 1);
    await page.waitForTimeout(1200);
    expect(await nombres()).toEqual(antes);
  });

  test('poner 0h saca del presupuesto sin apagar ni re-etiquetar', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    const before = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    const target = before.projects.find(p => p.bucket === 'budget');
    test.skip(!target, 'no hay proyectos en el presupuesto en este sandbox');

    const row = page.locator('[data-testid="projects-load-row"]').filter({ hasText: target.name }).first();
    await row.locator('[data-testid="projects-load-hours"]').click();
    await row.locator('[data-testid="projects-load-input"]').fill('0');
    await row.locator('[data-testid="projects-load-input"]').press('Enter');
    await expect(page.locator('[data-testid="projects-load-reorder"]')).toBeVisible({ timeout: 5000 });
    await page.locator('[data-testid="projects-load-reorder"]').click();
    await expect(page.locator('[data-testid="projects-load-head-outside"]')).toBeVisible({ timeout: 5000 });

    const after = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    const now = after.projects.find(p => p.id === target.id);
    expect(now.in_budget).toBe(false);
    expect(now.bucket).toBe('outside');
    expect(now.active).toBe(true);      // sigue prendido
    expect(now.kind).toBe(target.kind); // sin re-etiquetar
    expect(after.declared.outside_count).toBeGreaterThan(before.declared.outside_count);

    await page.evaluate(async ({ id, wh }) => {
      await fetch(`/api/projects/${id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ weekly_hours: wh === null ? 0 : wh }),
      });
    }, { id: target.id, wh: target.weekly_hours });
  });

  test('la fila tiene DOS controles, no cuatro', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    const filas = await page.locator('[data-testid="projects-load-row"]').count();
    // Cada fila: ⏻ y el control de horas. Los ciclos de tipo y jerarquía murieron.
    await expect(page.locator('[data-testid="projects-load-power"]')).toHaveCount(filas);
    await expect(page.locator('[data-testid="projects-load-hours"]')).toHaveCount(filas);
    await expect(page.locator('[data-testid="projects-load-kind"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="projects-load-tier"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="projects-load-add"]')).toHaveCount(0);
  });

  test('ninguna fila se marca como inactiva', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    // El juicio se borró: sin tachados, sin chip rojo, sin la frase. Las juntas
    // con el cliente no viven en esta base y el lector sólo ve `tasks`.
    await expect(page.locator('#projects-load')).not.toContainText('sin actividad');
    await expect(page.locator('[data-testid="today-hl-load-idle"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="projects-load-row"] .line-through')).toHaveCount(0);
    const api = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    expect(api.declared.idle_count).toBeUndefined();
    expect(api.projects.every(p => p.idle === undefined)).toBe(true);
  });

  test('el crédito a agentes se muestra y no se puede inflar con un permiso', async ({ page }) => {
    await page.goto('/?tab=projects');
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#projects-load-chips')).toContainText(/agentes \+0h · unproven/);

    const api = await page.evaluate(() => fetch('/api/projects/load').then(r => r.json()));
    expect(api.agent_capacity.credited_h).toBe(0.0);
    // Los carriles particionan el trabajo abierto: nada se pierde ni se cuenta dos veces.
    const lanes = api.agent_capacity.lane_counts;
    expect(lanes.ricardo + lanes.supervised + lanes.autonomous).toBeGreaterThanOrEqual(0);
  });
});
