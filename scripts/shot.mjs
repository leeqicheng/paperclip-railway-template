// agent-tools/shot.mjs
// Usage: node /opt/agent-tools/shot.mjs <path> [width] [height] [outfile]
// Example: node /opt/agent-tools/shot.mjs /ai/command-center 1440 900 /tmp/cc-1440.png
// Logs in to NON-PROD with the test account via the API, then takes a full-page screenshot.
import { chromium } from 'playwright';

const base = process.env.CARFLEET_NONPROD_URL;
const user = process.env.CARFLEET_NONPROD_TEST_USER;
const pass = process.env.CARFLEET_NONPROD_TEST_PASSWORD;
const [pagePath = '/', width = '1440', height = '900', out = '/tmp/shot.png'] = process.argv.slice(2);

if (!base || !user || !pass) {
  console.error('Missing CARFLEET_NONPROD_URL / _TEST_USER / _TEST_PASSWORD env vars.');
  process.exit(2);
}
if (base.includes('fleet.rotorautosg.com')) {
  console.error('Refusing to log in on PROD.');
  process.exit(2);
}

const res = await fetch(`${base}/api/auth/login`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: user, password: pass }),
});
if (!res.ok) {
  console.error(`Login failed: HTTP ${res.status}`);
  process.exit(1);
}
const match = (res.headers.get('set-cookie') || '').match(/cfai_session=([^;]+)/);
if (!match) {
  console.error('Login succeeded but no cfai_session cookie was returned.');
  process.exit(1);
}

const browser = await chromium.launch();
try {
  const ctx = await browser.newContext({ viewport: { width: Number(width), height: Number(height) } });
  await ctx.addCookies([{ name: 'cfai_session', value: match[1], url: base }]);
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  const resp = await page.goto(new URL(pagePath, base).toString(), { waitUntil: 'networkidle', timeout: 30000 });
  await page.screenshot({ path: out, fullPage: true });
  console.log(`OK ${resp?.status()} ${pagePath} ${width}x${height} -> ${out}`);
  if (errors.length) console.log(`Console errors (${errors.length}):\n- ` + errors.slice(0, 10).join('\n- '));
} finally {
  await browser.close();
}
