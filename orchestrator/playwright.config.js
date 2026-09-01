// Playwright config for the dashboard e2e spec (tests/board-empty-state.spec.js).
// By default it auto-starts a test-only server (a wiped DB copy) via webServer.
// Set PW_BASE_URL to run against an already-running server instead (handy where
// the process supervisor won't let the test spawn its own).
const { defineConfig } = require('@playwright/test');

const PORT = process.env.TEST_PORT || '8931';
const externalBase = process.env.PW_BASE_URL; // e.g. http://127.0.0.1:8931

module.exports = defineConfig({
  testDir: 'tests',
  testMatch: /(board-|crm-|drawer-|today-|threads-|workspaces|routing|projects|lakehouse|growth|whatsapp-|suggestions|cogload-).*\.spec\.js/,   // dashboard workspace + interaction contracts
  timeout: 30000,
  fullyParallel: false,
  workers: 1,                        // one shared server → run specs serially, no state races
  reporter: 'line',
  use: {
    baseURL: externalBase || `http://127.0.0.1:${PORT}`,
    headless: true,
  },
  webServer: externalBase ? undefined : {
    command: `.venv/bin/python tests/serve_test_dashboard.py`,
    url: `http://127.0.0.1:${PORT}/`,
    timeout: 30000,
    reuseExistingServer: true,
    env: { TEST_PORT: PORT, PYTHONPATH: process.cwd() },
  },
});
