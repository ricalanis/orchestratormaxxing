/*
 * Production-auth lifecycle contract for dashboard/static/auth-injector.js.
 *
 * A dashboard tab can outlive a service restart or token rotation.  The first
 * rejected mutation is still side-effect free (auth middleware runs before the
 * handler), so the browser may refresh its rendered token and replay once.
 *
 * Run: node --test tests/auth-injector.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dir = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  path.join(dir, '..', 'dashboard', 'static', 'auth-injector.js'), 'utf8');

function boot(initialToken, handler) {
  const dom = new JSDOM(
    `<!doctype html><meta name="dashboard-token" content="${initialToken}">`,
    { url: 'https://hermes.test/?tab=today', runScripts: 'outside-only' });
  dom.window.Headers = globalThis.Headers;
  dom.window.fetch = handler;
  dom.window.eval(source);
  return dom;
}

function authorization(init) {
  return new Headers((init && init.headers) || {}).get('Authorization');
}

test('a stale token refreshes from current HTML and replays the API mutation once', async () => {
  const calls = [];
  const dom = boot('token-a', async (input, init = {}) => {
    calls.push({ input: String(input), method: init.method || 'GET', auth: authorization(init) });
    if (calls.length === 1) return { status: 401, ok: false };
    if (calls.length === 2) return {
      status: 200, ok: true,
      text: async () => '<meta name="dashboard-token" content="token-b">',
    };
    return { status: 200, ok: true };
  });

  const response = await dom.window.fetch('/api/tasks/t_fixture/plan', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ planned_for: 'today' }),
  });

  assert.equal(response.status, 200);
  assert.deepEqual(calls, [
    { input: '/api/tasks/t_fixture/plan', method: 'PATCH', auth: 'Bearer token-a' },
    { input: 'https://hermes.test/', method: 'GET', auth: null },
    { input: '/api/tasks/t_fixture/plan', method: 'PATCH', auth: 'Bearer token-b' },
  ]);
  assert.equal(
    dom.window.document.querySelector('meta[name="dashboard-token"]').content,
    'token-b');
});

test('an initially empty token bootstraps after the side-effect-free 401', async () => {
  const auths = [];
  const dom = boot('', async (input, init = {}) => {
    auths.push(authorization(init));
    if (auths.length === 1) return { status: 401, ok: false };
    if (auths.length === 2) return {
      status: 200, ok: true,
      text: async () => '<meta name="dashboard-token" content="token-live">',
    };
    return { status: 204, ok: true };
  });

  const response = await dom.window.fetch('/api/day-plan', {
    method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' },
  });

  assert.equal(response.status, 204);
  assert.deepEqual(auths, [null, null, 'Bearer token-live']);
});

test('unchanged or rejected refresh never loops', async () => {
  const calls = [];
  const dom = boot('same-token', async (input, init = {}) => {
    calls.push({ input: String(input), auth: authorization(init) });
    if (calls.length === 1) return { status: 401, ok: false };
    return {
      status: 200, ok: true,
      text: async () => '<meta name="dashboard-token" content="same-token">',
    };
  });

  const response = await dom.window.fetch('/api/day-plan', { method: 'POST' });
  assert.equal(response.status, 401);
  assert.equal(calls.length, 2, 'one refresh, zero replay with an unchanged token');
});

test('explicit authorization is preserved and is never auto-recovered', async () => {
  const calls = [];
  const dom = boot('page-token', async (input, init = {}) => {
    calls.push(authorization(init));
    return { status: 401, ok: false };
  });

  await dom.window.fetch('/api/day-plan', {
    method: 'POST', headers: { Authorization: 'Bearer caller-token' },
  });
  assert.deepEqual(calls, ['Bearer caller-token']);
});

test('GETs and mutations outside the same-origin API never trigger recovery', async () => {
  const calls = [];
  const dom = boot('page-token', async (input, init = {}) => {
    calls.push({ input: String(input), auth: authorization(init) });
    return { status: 401, ok: false };
  });

  await dom.window.fetch('/api/day-plan');
  await dom.window.fetch('https://example.test/api/write', { method: 'POST' });
  await dom.window.fetch('/form-submit', { method: 'POST' });

  assert.deepEqual(calls, [
    { input: '/api/day-plan', auth: null },
    { input: 'https://example.test/api/write', auth: 'Bearer page-token' },
    { input: '/form-submit', auth: 'Bearer page-token' },
  ]);
});
