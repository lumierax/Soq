import { test, expect } from '@playwright/test';

const offline = { market: 'spot', horizon: 'swing', source: 'binance', connection: 'offline', updatedAt: null, totalSymbols: 0, analyzedSymbols: 0, scanProgress: 0, btcRegime: 'unknown', items: [], error: 'Binance غير متاح من موقع الخادم (HTTP 451).' };

test.beforeEach(async ({ page }) => {
  // These regressions exercise the explicit remote-backend mode. Direct
  // Binance mode is verified separately in direct-market.spec.ts.
  await page.addInitScript(() => localStorage.setItem('nabd.apiBase', 'http://127.0.0.1:5173'));
  await page.route('**/api/markets?**', route => route.fulfill({ json: offline }));
});

test('offline feed never silently becomes synthetic or live', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('Binance غير متاح من موقع الخادم (HTTP 451).')).toBeVisible();
  await expect(page.locator('.live-status')).toContainText('غير متصل');
  await expect(page.locator('.market-row')).toHaveCount(0);
  await expect(page.locator('canvas')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('explicit preview has labels, chart, search and correct per-market frames', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'معاينة التصميم' }).first().click();
  await expect(page.locator('.preview-banner')).toContainText('اصطناعية');
  await expect(page.locator('canvas').first()).toBeVisible();
  await expect(page.locator('.market-row')).toHaveCount(12);
  await page.getByRole('searchbox', { name: 'البحث عن كل أزواج USDT' }).fill('SOL');
  await expect(page.locator('.market-row')).toHaveCount(1);
  await page.getByRole('button', { name: 'عرض تحليل SOL' }).click();
  await expect(page.locator('.chart-panel .chart-header')).toContainText('SOL');
  await page.locator('.top-nav').getByRole('button', { name: /فيوتشر/ }).click();
  await expect(page.locator('.timeframe-picker')).toContainText('3 دقائق');
  await expect(page.locator('.timeframe-picker')).not.toContainText('أسبوع');
  await page.locator('.top-nav').getByRole('button', { name: /سبوت/ }).click();
  await expect(page.locator('.timeframe-picker')).toContainText('أسبوع');
});

test('preview cannot arm bot and token is never persisted', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'معاينة التصميم' }).first().click();
  await page.getByRole('button', { name: 'إعدادات الاتصال', exact: true }).click();
  const token = 'test-session-only-token';
  await page.locator('input[type=password]').fill(token);
  await page.getByRole('button', { name: 'حفظ الاتصال' }).click();
  await page.locator('.top-nav').getByRole('button', { name: /بوت التداول/ }).click();
  await expect(page.getByRole('button', { name: 'تشغيل البوت', exact: true })).toBeDisabled();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(token);
  await page.reload();
  await page.getByRole('button', { name: 'إعدادات الاتصال', exact: true }).click();
  await expect(page.locator('input[type=password]')).toHaveValue('');
});

test('mobile keeps content within viewport and dialog keyboard close works', async ({ page }) => {
  await page.setViewportSize({ width: 393, height: 852 });
  await page.goto('/');
  await page.getByRole('button', { name: 'معاينة التصميم' }).first().click();
  await expect(page.locator('.bottom-nav')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(393);
  await page.locator('.bottom-nav').getByRole('button', { name: 'الإعدادات' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('live arming needs typed confirmation and sends one intentional request', async ({ page }) => {
  const commands: unknown[] = [];
  const status = {
    state: 'stopped', mode: 'paper', credentialsConfigured: true, liveAllowed: true,
    config: { riskPercent: 1, maxPositions: 3, maxDailyLossPercent: 4, leverage: 3, minScore: 75, minRiskReward: 2, capital: 1000 },
    equity: 1000, dailyPnl: 0, positions: [], events: [], error: null,
  };
  await page.route('**/api/bot', route => route.fulfill({ json: status }));
  await page.route('**/api/bot/arm', async route => {
    commands.push(route.request().postDataJSON());
    await route.fulfill({ json: { ...status, state: 'running', mode: 'live' } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'إعدادات الاتصال', exact: true }).click();
  await page.locator('input[type=password]').fill('test-session-only-token');
  await page.getByRole('button', { name: 'حفظ الاتصال' }).click();
  await page.locator('.top-nav').getByRole('button', { name: /بوت التداول/ }).click();
  await page.getByRole('button', { name: 'تداول حقيقي', exact: true }).click();
  await page.getByRole('button', { name: 'تشغيل البوت', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  const confirm = page.getByRole('button', { name: 'تأكيد التداول الحقيقي', exact: true });
  await expect(confirm).toBeDisabled();
  expect(commands).toEqual([]);
  await page.getByPlaceholder('ENABLE LIVE TRADING').fill('ENABLE LIVE TRADING');
  await confirm.click();
  await expect(page.getByRole('button', { name: 'البوت يعمل', exact: true })).toBeVisible();
  expect(commands).toEqual([{ mode: 'live', confirmation: 'ENABLE LIVE TRADING' }]);
});
