const { test, expect } = require('@playwright/test');

// Network-only contract: the real UI renders against intercepted task payloads.
// No task POST/PATCH reaches the test DB (and therefore never the live CLI/DB).
test('due date is visible, editable, clearable, and sent on create', async ({ page }) => {
  let due = '2026-08-21';
  const patches = [];
  let createBody = null;

  await page.route('**/api/context/task/t_due_fixture', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        entity: {
          type: 'task', id: 't_due_fixture', title: 'Due fixture',
          status: 'backlog', assignee: 'ricardo', project_id: null,
          priority: 0, created_at: 1, due_date: due,
        },
        ancestors: [], children: [], ledger: [], events: [],
      }),
    });
  });
  await page.route('**/api/tasks/t_due_fixture', async route => {
    const body = route.request().postDataJSON();
    patches.push(body);
    due = body.due_date === '' ? null : body.due_date;
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"updated"}' });
  });
  await page.route('**/api/tasks', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    createBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: '{"status":"created","task_id":"t_created_due","warnings":[]}',
    });
  });

  await page.goto('/?tab=board');
  await page.waitForFunction(() => typeof openEntity === 'function');
  await page.evaluate(() => openEntity('task', 't_due_fixture'));

  const dueInput = page.getByTestId('ed-due-date');
  await expect(dueInput).toHaveValue('2026-08-21');
  await dueInput.fill('2026-08-25');
  await dueInput.dispatchEvent('change');
  await expect.poll(() => patches.at(-1)).toEqual({ due_date: '2026-08-25' });
  await expect(dueInput).toHaveValue('2026-08-25');

  await page.getByTestId('ed-due-clear').click();
  await expect.poll(() => patches.at(-1)).toEqual({ due_date: '' });
  await expect(dueInput).toHaveValue('');

  await page.evaluate(() => { closeEntity({ push: false }); openNewTaskModal('sprint_due_fixture'); });
  const createDue = page.locator('#nt-due');
  await expect(createDue).toBeVisible();
  await page.locator('#nt-title').fill('Created with a due date');
  await createDue.fill('2026-08-28');
  await page.locator('#nt-submit').click();
  await expect.poll(() => createBody && createBody.due_date).toBe('2026-08-28');
});

test('shared due badge distinguishes overdue, today, future, and terminal', async ({ page }) => {
  await page.goto('/?tab=board');
  await page.waitForFunction(() => typeof dueDateBadge === 'function');
  const result = await page.evaluate(() => {
    const today = new Date();
    const iso = d => [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-');
    const past = new Date(today); past.setDate(today.getDate() - 1);
    const future = new Date(today); future.setDate(today.getDate() + 1);
    return {
      none: dueDateBadge({ id: 'n', due_date: null, status: 'backlog' }),
      past: dueDateBadge({ id: 'p', due_date: iso(past), status: 'backlog' }),
      today: dueDateBadge({ id: 't', due_date: iso(today), status: 'backlog' }),
      future: dueDateBadge({ id: 'f', due_date: iso(future), status: 'backlog' }),
      done: dueDateBadge({ id: 'd', due_date: iso(past), status: 'done' }),
    };
  });

  expect(result.none).toBe('');
  for (const key of ['past', 'today', 'future', 'done'])
    expect(result[key]).toContain('data-testid="task-due-date"');
  expect(result.past).toContain('red');
  expect(result.today).toContain('amber');
  expect(result.future).not.toContain('red');
  expect(result.done).not.toContain('red');
});
