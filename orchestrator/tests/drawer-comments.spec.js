// e2e for the task-comments section in the entity drawer. The drawer (fed by
// /api/context/task/{id}) grows a Comments section at the bottom for tasks:
// textarea + Add Comment, live-refresh on post, per-comment delete. Backed by
// POST/GET/DELETE /api/tasks/{id}/comments. Runs against the wiped-DB copy from
// playwright.config.js, so it may write freely.
//
// Run: `npx playwright test drawer-comments`.
const { test, expect } = require('@playwright/test');

const DRAWER = '#entity-drawer';
const COMMENTS = '#ed-comments';

const firstTaskId = (page) =>
  page.evaluate(async () => {
    const tasks = (await fetch('/api/tasks?limit=0').then((r) => r.json())).tasks || [];
    return (tasks[0] || {}).id;
  });

test.describe('Drawer comments section', () => {
  test('post → appears → delete → gone', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const taskId = await firstTaskId(page);
    expect(taskId, 'no task id available in the test DB').toBeTruthy();

    await page.goto(`/?entity=task:${encodeURIComponent(taskId)}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator(DRAWER)).toBeVisible();

    // The Comments section + its composer are present for a task.
    const section = page.locator(COMMENTS);
    await expect(section).toBeVisible();
    const input = page.locator('#ed-comment-input');
    await expect(input).toBeVisible();

    // Post a unique comment.
    const body = `pw-comment-${Date.now()}`;
    await input.fill(body);
    await page.locator(`${COMMENTS} button`, { hasText: 'Add Comment' }).click();

    // It shows up (live-refresh, no full page reload), with its author.
    const line = page.locator(COMMENTS).getByText(body, { exact: true });
    await expect(line).toBeVisible();
    await expect(page.locator(COMMENTS)).toContainText('ricardo');
    // The textarea is cleared after a successful post.
    await expect(input).toHaveValue('');

    // Delete it via the row's ✕ (hover-revealed but clickable) → it disappears.
    const row = page.locator(`${COMMENTS} .group`, { has: page.getByText(body, { exact: true }) });
    await row.locator('button[aria-label="Delete comment"]').click();
    await expect(page.locator(COMMENTS).getByText(body, { exact: true })).toHaveCount(0);
  });

  test('non-task entities have no comments section', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const projectId = await page.evaluate(async () =>
      ((await fetch('/api/projects').then((r) => r.json()))[0] || {}).id);
    test.skip(!projectId, 'no project in the test DB');

    await page.goto(`/?entity=project:${encodeURIComponent(projectId)}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator(DRAWER)).toBeVisible();
    await expect(page.locator(COMMENTS)).toHaveCount(0);
  });

  test('Ubuntu routes safe Bear note links to Bear Web and rejects other actions', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const noteId = '8C5F2CA0-1EDE-4A9A-A038-2F33C65A85A5';
    const bearUrl = `bear://x-callback-url/open-note?id=${noteId}`;

    const historyHtml = await page.evaluate(({ bearUrl }) => {
      const box = document.createElement('div');
      box.id = 'ed-comments';
      document.body.appendChild(box);
      edRenderComments('t_bear_probe', [{
        id: 1,
        author: 'ricardo',
        body: `${bearUrl}\n<img src=x onerror=alert(1)>\nbear://x-callback-url/create?id=${bearUrl.split('=')[1]}\nbear://x-callback-url/open-note?id=7E4B681B&x-success=javascript:alert(1)`,
        created_at: 1,
      }]);
      return historyDetail({ kind: 'commented', payload: { text: bearUrl } });
    }, { bearUrl });

    const section = page.locator(COMMENTS);
    const link = section.locator('[data-bear-note-link]');
    await expect(link).toHaveCount(1);
    await expect(link).toHaveAttribute(
      'href',
      `https://web.bear.app/#/notes/note/${noteId}`,
    );
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(section.locator('img')).toHaveCount(0);
    await expect(section).toContainText('<img src=x onerror=alert(1)>');
    expect(historyHtml).toContain(`https://web.bear.app/#/notes/note/${noteId}`);
  });

  test('a Mac browser keeps the native Bear open-note URL', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(Navigator.prototype, 'userAgentData', {
        configurable: true,
        get: () => ({ platform: 'macOS' }),
      });
      Object.defineProperty(Navigator.prototype, 'platform', {
        configurable: true,
        get: () => 'MacIntel',
      });
    });
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const bearUrl = 'bear://x-callback-url/open-note?id=8C5F2CA0-1EDE-4A9A-A038-2F33C65A85A5&header=Next%20step';

    await page.evaluate(({ bearUrl }) => {
      const box = document.createElement('div');
      box.id = 'ed-comments';
      document.body.appendChild(box);
      edRenderComments('t_bear_probe', [{
        id: 1,
        author: 'ricardo',
        body: bearUrl,
        created_at: 1,
      }]);
    }, { bearUrl });

    const link = page.locator(`${COMMENTS} [data-bear-note-link]`);
    await expect(link).toHaveAttribute('href', bearUrl);
    await expect(link).not.toHaveAttribute('target', '_blank');
  });
});
