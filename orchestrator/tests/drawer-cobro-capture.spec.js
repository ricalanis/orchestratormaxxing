/*
 * m17/F3 — the cash plan's capture gestures in the deal drawer.
 *
 * Every input rides a gesture that already exists: won → terms chips,
 * 💵 → inline confirm with the expected date PREFILLED (invoiced+términos;
 * fixed-cycle accounts → next 10th/20th), ✅ → the reconciliation delta in the
 * toast, and the permanent audited «📅 Cobro esperado» control (first set
 * free, moving a date requires a reason, frozen once paid). All responses
 * are intercepted — the spec owns its state and never writes real money.
 *
 * Run: `npx playwright test drawer-cobro-capture --workers=1`
 */
const { test, expect } = require('@playwright/test');

const ENTITY = (over = {}) => ({
  type: 'deal', id: 'd_cap', title: 'Deal captura', stage: 'won',
  value: 18500, currency: 'MXN', account_id: 'acct_cap',
  account_name: 'Acme', project_id: null, initiative_id: null,
  invoiced_at: null, paid_at: null,
  payment_terms_days: null, expected_payment_date: null,
  expected_payment_date_original: null,
  deliver: { default: null, projects: [] },
  ...over,
});

function isoDaysFromNow(days) {
  const t = new Date();
  const d = new Date(t.getFullYear(), t.getMonth(), t.getDate() + days);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
    + '-' + String(d.getDate()).padStart(2, '0');
}

async function openDrawer(page, entity, hooks = {}) {
  await page.route('**/api/context/deal/*', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ entity, ancestors: [], children: [], actions: [] }),
  }));
  if (hooks.routes) await hooks.routes(page);
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => openEntity('deal', 'd_cap'));
  await expect(page.locator('[data-testid="ed-promise-edit"], #ed-promise-cell')
    .first()).toBeVisible({ timeout: 10000 });
}

test('an orphan won deal now has the 💵 verb, wearing its sin-entregar badge', async ({ page }) => {
  await openDrawer(page, ENTITY());
  await expect(page.locator('[data-testid="ed-invoiced-btn"]')).toBeVisible();
  await expect(page.locator('[data-testid="ed-invoiced-undelivered"]')).toHaveText('sin entregar');
});

test('the 💵 tap opens the inline confirm prefilled from the terms and POSTs the date', async ({ page }) => {
  let posted = null;
  await openDrawer(page, ENTITY({ payment_terms_days: 30 }), {
    routes: async (p) => {
      await p.route('**/api/crm/deals/d_cap/invoiced', route => {
        posted = route.request().postDataJSON();
        return route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ status: 'deal_invoiced', deal_id: 'd_cap',
            invoiced_at: 1754000000, expected_payment_date: posted.expected_payment_date,
            expected_derived: false }) });
      });
    },
  });
  await page.locator('[data-testid="ed-invoiced-btn"]').click();
  const input = page.locator('[data-testid="ed-invoiced-date"]');
  await expect(input).toBeVisible();
  await expect(input).toHaveValue(isoDaysFromNow(30));
  await page.locator('[data-testid="ed-invoiced-confirm"]').click();
  await expect.poll(() => posted).not.toBeNull();
  expect(posted.expected_payment_date).toBe(isoDaysFromNow(30));
});

test('terms chips appear only while unset and PATCH the integer', async ({ page }) => {
  let patched = null;
  await openDrawer(page, ENTITY(), {
    routes: async (p) => {
      await p.route('**/api/crm/deals/d_cap', route => {
        if (route.request().method() !== 'PATCH') return route.fallback();
        patched = route.request().postDataJSON();
        return route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ status: 'updated' }) });
      });
    },
  });
  const chips = page.locator('[data-testid="ed-terms-chip"]');
  await expect(chips).toHaveCount(4);
  await chips.filter({ hasText: '30d' }).click();
  await expect.poll(() => patched).not.toBeNull();
  expect(patched.payment_terms_days).toBe(30);
});

test('the ✅ toast carries the computed reconciliation delta', async ({ page }) => {
  await openDrawer(page, ENTITY({ invoiced_at: 1754000000,
    expected_payment_date: isoDaysFromNow(-4),
    expected_payment_date_original: isoDaysFromNow(-4) }), {
    routes: async (p) => {
      await p.route('**/api/crm/deals/d_cap/paid', route => route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'deal_paid', deal_id: 'd_cap',
          paid_at: 1754000000, delta_days: 4, delta_original_days: 4 }) }));
    },
  });
  // m19: the tap now opens the amount confirm first (prefilled = value).
  await page.locator('[data-testid="ed-paid-btn"]').click();
  await page.locator('[data-testid="ed-paid-confirm"]').click();
  await expect(page.locator('#toast-zone, .toast, [role="status"]').filter({
    hasText: '+4d tarde' }).first()).toBeVisible({ timeout: 5000 });
});

test('the permanent promise control: moving a date sends the reason to the audited verb', async ({ page }) => {
  let promised = null;
  await openDrawer(page, ENTITY({ invoiced_at: 1754000000,
    expected_payment_date: isoDaysFromNow(5),
    expected_payment_date_original: isoDaysFromNow(5) }), {
    routes: async (p) => {
      await p.route('**/api/crm/deals/d_cap/payment-promise', route => {
        promised = route.request().postDataJSON();
        return route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ status: 'payment_repromised', deal_id: 'd_cap',
            expected_payment_date: promised.expected_payment_date }) });
      });
    },
  });
  await page.locator('[data-testid="ed-promise-edit"]').click();
  await page.locator('[data-testid="ed-promise-date"]').fill(isoDaysFromNow(12));
  await page.locator('[data-testid="ed-promise-reason"]').fill('cliente pidió mover al corte');
  await page.locator('[data-testid="ed-promise-save"]').click();
  await expect.poll(() => promised).not.toBeNull();
  expect(promised.expected_payment_date).toBe(isoDaysFromNow(12));
  expect(promised.reason).toBe('cliente pidió mover al corte');
});

test('a paid deal shows the plan frozen — no edit affordance survives ✅', async ({ page }) => {
  await openDrawer(page, ENTITY({ invoiced_at: 1754000000, paid_at: 1754100000,
    expected_payment_date: '2026-08-01', expected_payment_date_original: '2026-08-01' }));
  await expect(page.locator('#ed-promise-cell')).toContainText('congelado');
  await expect(page.locator('[data-testid="ed-promise-edit"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="ed-invoiced-btn"]')).toHaveCount(0);
});

test('the fixed pay-cycle prefill suggests the next 10th/20th payday', async ({ page }) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  const out = await page.evaluate(() => {
    window.TENANT_UI = { payday_1020_re: 'cliente 1020' };  // the cycle list is tenant config
    return {
      d05: (() => { const d = edNextPayday1020(new Date(2026, 7, 5)); return [d.getMonth(), d.getDate()]; })(),
      d14: (() => { const d = edNextPayday1020(new Date(2026, 7, 14)); return [d.getMonth(), d.getDate()]; })(),
      d25: (() => { const d = edNextPayday1020(new Date(2026, 7, 25)); return [d.getMonth(), d.getDate()]; })(),
      ens: edSuggestExpected({ account_name: 'Cliente 1020 SA', payment_terms_days: 30 }),
      terms: edSuggestExpected({ account_name: 'Acme', payment_terms_days: 0 }),
      none: edSuggestExpected({ account_name: 'Acme' }),
    };
  });
  expect(out.d05).toEqual([7, 10]);   // Aug 5  → Aug 10
  expect(out.d14).toEqual([7, 20]);   // Aug 14 → Aug 20
  expect(out.d25).toEqual([8, 10]);   // Aug 25 → Sep 10
  expect(/^\d{4}-\d{2}-\d{2}$/.test(out.ens)).toBe(true);
  expect(out.ens.slice(8)).toMatch(/^(10|20)$/);  // pay cycle wins over terms
  const today = new Date();
  const isoToday = today.getFullYear() + '-' + String(today.getMonth() + 1).padStart(2, '0')
    + '-' + String(today.getDate()).padStart(2, '0');
  expect(out.terms).toBe(isoToday);   // contado → hoy
  expect(out.none).toBe('');          // sin términos → sin sugerencia
});

test('the 🧾 launch control PATCHes expected_invoice_date without a reason', async ({ page }) => {
  let patched = null;
  await openDrawer(page, ENTITY(), {
    routes: async (p) => {
      await p.route('**/api/crm/deals/d_cap', route => {
        if (route.request().method() !== 'PATCH') return route.fallback();
        patched = route.request().postDataJSON();
        return route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ status: 'updated' }) });
      });
    },
  });
  await page.locator('[data-testid="ed-launch-edit"]').click();
  await page.locator('[data-testid="ed-launch-date"]').fill(isoDaysFromNow(2));
  await page.locator('[data-testid="ed-launch-save"]').click();
  await expect.poll(() => patched).not.toBeNull();
  expect(patched.expected_invoice_date).toBe(isoDaysFromNow(2));
});

test('the 🧾 row disappears once invoiced — the launch already happened', async ({ page }) => {
  await openDrawer(page, ENTITY({ invoiced_at: 1754000000 }));
  await expect(page.locator('[data-testid="ed-launch-edit"]')).toHaveCount(0);
});

test('the ✅ tap opens the amount confirm prefilled with value and POSTs paid_amount', async ({ page }) => {
  let posted = null;
  await openDrawer(page, ENTITY({ invoiced_at: 1754000000, value: 18500 }), {
    routes: async (p) => {
      await p.route('**/api/crm/deals/d_cap/paid', route => {
        posted = route.request().postDataJSON();
        return route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ status: 'deal_paid', deal_id: 'd_cap',
            paid_at: 1754000000, paid_amount: posted.paid_amount }) });
      });
    },
  });
  await page.locator('[data-testid="ed-paid-btn"]').click();
  const input = page.locator('[data-testid="ed-paid-amount"]');
  await expect(input).toBeVisible();
  await expect(input).toHaveValue('18500');
  await input.fill('18000');
  await page.locator('[data-testid="ed-paid-confirm"]').click();
  await expect.poll(() => posted).not.toBeNull();
  expect(posted.paid_amount).toBe(18000);
});

test('a paid deal shows the real deposit and its diff vs pactado', async ({ page }) => {
  await openDrawer(page, ENTITY({ invoiced_at: 1754000000, paid_at: 1754100000,
    value: 18500, paid_amount: 18000,
    expected_payment_date: '2026-08-01', expected_payment_date_original: '2026-08-01' }));
  const body = page.locator('#entity-drawer, [data-testid="entity-drawer"], #context-drawer').first();
  await expect(page.getByText('18,000', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('vs pactado', { exact: false }).first()).toBeVisible();
});
