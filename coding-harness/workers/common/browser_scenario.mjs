// One browser scenario on the harness's shared browser (common/browser_service), installed next to
// Playwright and handed to every session and check run as PLAYWRIGHT_SCENARIO.
//
//   const { scenario } = await import(process.env.PLAYWRIGHT_SCENARIO);
//   await scenario(async ({ page }) => { /* drive and assert */ }, {
//     start: ['bash', 'start.sh'], ports: ['BACKEND_PORT', 'FRONTEND_PORT'],
//     url: 'http://127.0.0.1:{FRONTEND_PORT}', viewport: { width: 1280, height: 800 } });
//
// The harness owns the parts every scenario needs and that went wrong when each script wrote its own:
// - the connection: it connects, opens a context and page, and always closes the connection. A script
//   that closed only its context kept Node alive after it PASSED, and the delivery audit waited on it
//   for 8 hours (run 8064c234).
// - the app: `start` runs in the foreground of the scenario, readiness is an HTTP answer from `url`
//   (never text on the page), and the app is stopped afterwards. A script that waited for a heading's
//   text swallowed Playwright's "matched 2 elements" error in its retry loop, reported "application
//   was not ready", and the team rebuilt a working launcher for three hours (run 3e3991fc).
// - the ports: every name in `ports` gets a free port for THIS run, passed to the app in its
//   environment and substituted for `{NAME}` in `url`. Fixed ports collided whenever two sessions,
//   an evaluation or another run started the app at once, and the loser's "port in use" sent agents
//   to write port-reuse logic into the launcher (run 3e3991fc).
// - the verdict: a thrown error prints one line (plus the app's log tail when the app never answered)
//   and sets exit code 1; the body may also set process.exitCode itself.
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE + '/index.mjs');
const READY_ATTEMPTS = 240; // x 0.5 s: a cold Maven or npm start included

function startApp(start, env) {
  const log = [];
  const child = spawn(start[0], start.slice(1), {
    cwd: process.cwd(), detached: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ...(env || {}) },
  });
  for (const s of [child.stdout, child.stderr]) s.on('data', c => { log.push(String(c)); if (log.length > 400) log.shift(); });
  child.on('error', e => log.push(`could not start ${start.join(' ')}: ${e.message}\n`));
  return { child, tail: () => log.join('').slice(-1500) };
}

function stopApp(app) {
  if (!app) return;
  try { process.kill(-app.child.pid, 'SIGTERM'); } catch {}
  app.child.stdout?.destroy(); app.child.stderr?.destroy(); app.child.unref();
}

async function waitForApp(url, app) {
  let last = '';
  for (let i = 0; i < READY_ATTEMPTS; i += 1) {
    if (app && app.child.exitCode !== null) {
      throw new Error(`the app exited (code ${app.child.exitCode}) before ${url} answered:\n${app.tail()}`);
    }
    try {
      const r = await fetch(url);
      if (r.status < 500) return;
      last = `HTTP ${r.status}`;
    } catch (e) { last = e.cause?.code || e.message; }
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error(`${url} never answered (${last})${app ? `; app log:\n${app.tail()}` : ''}`);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => { const { port } = server.address(); server.close(() => resolve(port)); });
  });
}

export async function scenario(body, { viewport, start, env, url, ports } = {}) {
  let browser, app;
  try {
    const given = {};
    for (const name of ports || []) given[name] = String(await freePort());
    if (url) url = url.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (m, name) => given[name] ?? m);
    if (start) app = startApp(start, { ...(env || {}), ...given });
    if (url) await waitForApp(url, app);
    browser = await chromium.connect(process.env.PLAYWRIGHT_WS_ENDPOINT);
    const context = await browser.newContext(viewport ? { viewport } : {});
    const page = await context.newPage();
    if (url) await page.goto(url);
    await body({ page, context, browser, url, ports: given });
    if (process.exitCode === undefined) process.exitCode = 0;
  } catch (error) {
    console.error(`scenario failed: ${error?.message ?? error}`);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
    stopApp(app);
  }
}
