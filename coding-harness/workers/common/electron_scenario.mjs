// One Electron scenario on the host (common/device_service.py `device electron`): Playwright's Electron driver launches
// the tree's own Electron (`npm install electron` in the tree) on the app's main entry, and the scenario module's
// default export (or `scenario`) gets { app, page } where page is the first window:
//
//   export default async ({ app, page }) => { await page.getByRole('button', { name: 'Open' }).click(); ... };
//
// A thrown error prints one line and exits 1; the app is always closed, so nothing is left running.
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [scenarioPath, main] = process.argv.slice(2);
const { _electron } = await import(process.env.PLAYWRIGHT_MODULE + '/index.mjs');
let executablePath;
try {
  executablePath = createRequire(path.join(process.cwd(), 'package.json'))('electron');
} catch {
  console.error('FAIL: electron is not installed in this tree (npm install --save-dev electron)');
  process.exit(1);
}
let app;
try {
  app = await _electron.launch({ executablePath, args: [main], cwd: process.cwd() });
  const page = await app.firstWindow();
  const mod = await import(pathToFileURL(scenarioPath).href);
  const run = mod.default || mod.scenario;
  if (typeof run !== 'function') throw new Error(`${scenarioPath} exports no scenario function`);
  await run({ app, page });
  console.log('PASS');
} catch (e) {
  console.error('FAIL: ' + String(e && e.message || e).split('\n')[0]);
  process.exitCode = 1;
} finally {
  if (app) await app.close().catch(() => {});
}
