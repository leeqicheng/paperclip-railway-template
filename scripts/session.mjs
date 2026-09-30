// scripts/session.mjs  →  copied to /opt/agent-tools/session.mjs in the Docker image
// Logged-in headless browser for NON-PROD testing. Agents import it from their own test script:
//
//   import { openNonprod } from '/opt/agent-tools/session.mjs';
//   const s = await openNonprod({ width: 1440, height: 900 });
//   try {
//     await s.go('/ai/command-center');
//     await s.page.getByRole('button', { name: 'Failed' }).click();
//     console.log('heading:', await s.page.locator('h1').first().innerText());
//   } finally {
//     await s.report();   // prints console errors + failed requests
//     await s.close();
//   }
//
// Never prints the password or the session cookie. Refuses to run against prod and blocks any request to prod.
import { chromium } from 'playwright';

export { chromium };

const PROD_HOSTS = ['fleet.rotorautosg.com'];

export async function openNonprod({ width = 1440, height = 900 } = {}) {
  const base = process.env.CARFLEET_NONPROD_URL;
  const user = process.env.CARFLEET_NONPROD_TEST_USER;
  const pass = process.env.CARFLEET_NONPROD_TEST_PASSWORD;
  if (!base || !user || !pass) {
    throw new Error('Missing CARFLEET_NONPROD_URL / CARFLEET_NONPROD_TEST_USER / CARFLEET_NONPROD_TEST_PASSWORD');
  }
  const baseHost = new URL(base).hostname;
  if (PROD_HOSTS.some((h) => baseHost.endsWith(h))) throw new Error('Refusing to run against PROD');

  const res = await fetch(new URL('/api/auth/login', base), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: user, password: pass }),
  });
  if (!res.ok) throw new Error(`Login failed: HTTP ${res.status}`);
  const match = (res.headers.get('set-cookie') || '').match(/cfai_session=([^;]+)/);
  if (!match) throw new Error('Login succeeded but no cfai_session cookie was returned');

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width, height } });
  await context.addCookies([{ name: 'cfai_session', value: match[1], url: base }]);

  // Hard block: never let the browser reach prod, even via links or redirects.
  await context.route('**/*', (route) => {
    const host = new URL(route.request().url()).hostname;
    return PROD_HOSTS.some((h) => host.endsWith(h)) ? route.abort() : route.continue();
  });

  const page = await context.newPage();
  const consoleErrors = [];
  const failedRequests = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));
  page.on('response', (r) => {
    if (r.status() >= 400) failedRequests.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`);
  });

  const go = (path, opts = {}) =>
    page.goto(new URL(path, base).toString(), { waitUntil: 'networkidle', timeout: 30000, ...opts });

  const report = async () => {
    console.log(`Console errors (${consoleErrors.length}):` + (consoleErrors.length ? '\n- ' + consoleErrors.slice(0, 15).join('\n- ') : ' none'));
    console.log(`Failed requests (${failedRequests.length}):` + (failedRequests.length ? '\n- ' + failedRequests.slice(0, 15).join('\n- ') : ' none'));
  };

  return { browser, context, page, go, base, consoleErrors, failedRequests, report, close: () => browser.close() };
}
