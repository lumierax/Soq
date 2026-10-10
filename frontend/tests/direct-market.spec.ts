import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

interface ExchangeSymbol {
  symbol: string;
  baseAsset: string;
  quoteAsset: string;
  status: string;
  isSpotTradingAllowed: boolean;
  contractType: string;
  marginAsset: string;
  filters: { filterType: string; [key: string]: string }[];
}

function symbol(baseAsset: string, values: Partial<ExchangeSymbol> = {}): ExchangeSymbol {
  return {
    symbol: `${baseAsset}USDT`, baseAsset, quoteAsset: 'USDT', status: 'TRADING',
    isSpotTradingAllowed: true, contractType: 'PERPETUAL', marginAsset: 'USDT',
    filters: [
      { filterType: 'PRICE_FILTER', tickSize: '0.01000000' },
      { filterType: 'LOT_SIZE', stepSize: '0.00100000', minQty: '0.00100000' },
      { filterType: 'MIN_NOTIONAL', minNotional: '5.00000000' },
    ],
    ...values,
  };
}

function ticker(entry: ExchangeSymbol) {
  return { symbol: entry.symbol, lastPrice: '123.45', priceChangePercent: '2.50', quoteVolume: '1000000', highPrice: '130', lowPrice: '120' };
}

async function publicMarket(page: Page, options: { spot?: () => ExchangeSymbol[]; futures?: () => ExchangeSymbol[]; unavailable?: boolean } = {}) {
  const backendRequests: string[] = [];
  const publicRequests: { url: string; headers: Record<string, string> }[] = [];
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  // Any accidental request to an old server API fails locally; no private
  // Binance request or real order can leave these browser tests.
  await page.route(/\/api\/(markets|candles|analysis|backtest|bot)(?:[/?]|$)/, route => {
    backendRequests.push(route.request().url());
    return route.fulfill({ status: 503, json: { detail: 'Unexpected backend request in direct mode' } });
  });
  await page.route(/https:\/\/(api|fapi)\.binance\.com\//, route => {
    const request = route.request();
    publicRequests.push({ url: request.url(), headers: request.headers() });
    if (options.unavailable) return route.fulfill({ status: 451, json: { code: 0, msg: 'Service unavailable from a restricted location' } });
    const url = new URL(request.url());
    const entries = url.hostname === 'fapi.binance.com' ? options.futures?.() ?? [] : options.spot?.() ?? [symbol('BTC')];
    if (url.pathname.endsWith('/exchangeInfo')) return route.fulfill({ json: { symbols: entries } });
    if (url.pathname.endsWith('/ticker/24hr')) return route.fulfill({ json: entries.map(ticker) });
    if (url.pathname.endsWith('/premiumIndex')) return route.fulfill({ json: entries.map(entry => ({ symbol: entry.symbol, lastFundingRate: '0.0001' })) });
    // Discovery is independently useful even when a new coin has too little
    // candle history to produce a verified recommendation.
    return route.fulfill({ json: [] });
  });
  await page.routeWebSocket(/wss:\/\/[^/]*binance\.(com|vision)/, socket => socket.close());
  return { backendRequests, publicRequests, pageErrors };
}

test('default direct discovery includes every active USDT pair and Unicode without API keys', async ({ page }) => {
  const active = [symbol('BTC'), ...Array.from({ length: 258 }, (_, index) => symbol(`COIN${String(index).padStart(3, '0')}`)), symbol('币安人生')];
  const excluded = [
    symbol('REMOVED', { status: 'BREAK' }),
    symbol('OTHERQUOTE', { quoteAsset: 'BTC', symbol: 'OTHERQUOTEBTC' }),
    symbol('NOSPOT', { isSpotTradingAllowed: false }),
  ];
  const requests = await publicMarket(page, { spot: () => [...active, ...excluded] });
  await page.goto('/');
  await expect(page.locator('.market-row')).toHaveCount(260);
  await expect(page.locator('.live-status')).toContainText('السوق مباشر');
  await page.getByRole('searchbox', { name: 'البحث عن كل أزواج USDT' }).fill('币安人生');
  await expect(page.locator('.market-row')).toHaveCount(1);
  await expect(page.locator('.market-row')).toContainText('123.45');
  await page.getByRole('searchbox', { name: 'البحث عن كل أزواج USDT' }).fill('COIN257');
  await expect(page.locator('.market-row')).toHaveCount(1);
  expect(requests.publicRequests.some(request => request.url.includes('/api/v3/exchangeInfo'))).toBe(true);
  expect(requests.publicRequests.every(request => !request.headers.authorization && !request.headers['x-mbx-apikey'])).toBe(true);
  expect(requests.backendRequests).toEqual([]);
  expect(requests.pageErrors).toEqual([]);
});

test('fresh Binance catalog discovers new coins and removes delisted coins', async ({ page }) => {
  let entries = [symbol('BTC'), symbol('OLDCOIN')];
  const requests = await publicMarket(page, { spot: () => entries });
  await page.goto('/');
  await expect(page.locator('.market-row')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'عرض تحليل OLDCOIN', exact: true })).toBeVisible();
  entries = [symbol('BTC'), symbol('OLDCOIN', { status: 'BREAK' }), symbol('NEWCOIN')];
  await page.reload();
  await expect(page.locator('.market-row')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'عرض تحليل NEWCOIN', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'عرض تحليل OLDCOIN', exact: true })).toHaveCount(0);
  expect(requests.backendRequests).toEqual([]);
});

test('futures discovery stays separate and includes only active USDT perpetuals', async ({ page }) => {
  const requests = await publicMarket(page, {
    spot: () => [symbol('SPOTONLY')],
    futures: () => [
      symbol('FUTURES'), symbol('币安合约'),
      symbol('DELIVERY', { contractType: 'CURRENT_QUARTER' }),
      symbol('INVERSE', { marginAsset: 'BTC' }),
      symbol('HALTED', { status: 'BREAK' }),
    ],
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'عرض تحليل SPOTONLY', exact: true })).toBeVisible();
  await page.locator('.top-nav').getByRole('button', { name: /فيوتشر/ }).click();
  await expect(page.locator('.market-row')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'عرض تحليل FUTURES', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'عرض تحليل 币安合约', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'عرض تحليل SPOTONLY', exact: true })).toHaveCount(0);
  await expect(page.locator('.timeframe-picker')).toContainText('3 دقائق');
  await expect(page.locator('.timeframe-picker')).not.toContainText('أسبوع');
  expect(requests.backendRequests).toEqual([]);
});

test('unavailable direct Binance never becomes a live or synthetic feed', async ({ page }) => {
  const requests = await publicMarket(page, { unavailable: true });
  await page.goto('/');
  await expect(page.locator('.error-banner')).toContainText('451');
  await expect(page.locator('.live-status')).toContainText('غير متصل');
  await expect(page.locator('.market-row')).toHaveCount(0);
  await expect(page.locator('canvas')).toHaveCount(0);
  await expect(page.locator('.preview-banner')).toHaveCount(0);
  expect(requests.backendRequests).toEqual([]);
  expect(requests.pageErrors).toEqual([]);
});

test('direct settings need no server or token and cannot arm the trading bot', async ({ page }) => {
  const requests = await publicMarket(page, { unavailable: true });
  await page.goto('/');
  await page.getByRole('button', { name: 'إعدادات الاتصال', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: 'مباشر من Binance', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(dialog).toContainText('لا تحتاج رابط خادم أو مفتاح API');
  await expect(dialog.locator('input')).toHaveCount(0);
  await dialog.getByRole('button', { name: 'حفظ الاتصال', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('nabd.apiBase'))).toBe('');
  await page.locator('.top-nav').getByRole('button', { name: /بوت التداول/ }).click();
  await expect(page.getByRole('button', { name: 'تشغيل البوت', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'تداول حقيقي', exact: true }).click();
  await expect(page.getByRole('button', { name: 'تشغيل البوت', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'حفظ الإعدادات', exact: true })).toBeDisabled();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(requests.backendRequests).toEqual([]);
});
