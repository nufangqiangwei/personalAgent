import { defineConfig, devices } from '@playwright/test'

// E2E runs against the production-like local app: FastAPI serves the built
// frontend and the API on 127.0.0.1. The webServer script seeds an isolated
// workspace under .cache/e2e-data, never the user's real data directory.
const port = Number(process.env.PERSONAL_AGENT_E2E_PORT ?? 8123)
const baseURL = `http://127.0.0.1:${port}`

const webServer = process.env.PERSONAL_AGENT_E2E_NO_SERVER
  ? undefined
  : {
      command:
        'powershell -NoProfile -ExecutionPolicy Bypass -File ../scripts/e2e-backend.ps1',
      url: `${baseURL}/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      cwd: __dirname,
      env: { PERSONAL_AGENT_E2E_PORT: String(port) },
    }

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  ...(webServer ? { webServer } : {}),
})
