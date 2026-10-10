import { expect, test } from '@playwright/test';
import { analyzeCandles, INTERVAL_MS, REQUIRED_TIMEFRAMES, regimeFromCandles, type CandleWithClose, type Interval } from '../../src/local-analysis';
import { LocalMarketService, publicTransport, type PublicHttpResult, type PublicTransport } from '../../src/local-market';
import type { Analysis, Horizon, Market } from '../../src/types';

const pause = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
const deferred = () => {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
};
const endOfClosed = (interval: Interval, now: number) => {
  const offset = interval === '1w' ? 4 * INTERVAL_MS['1d'] : 0;
  return Math.floor((now - offset) / INTERVAL_MS[interval]) * INTERVAL_MS[interval] + offset - 1;
};
interface Call { market: Market; path: string; params: URLSearchParams; at: number }

function fixture() {
  const state = {
    now: Date.UTC(2026, 9, 10, 12, 17, 59, 999),
    spot: ['BTCUSDT', 'ETHUSDT', '币USDT'],
    futures: ['BTCUSDT', 'ETHUSDT'],
    calls: [] as Call[], active: 0, maxActive: 0,
    tickerFailure: false, spotFailure: false, btcFailure: false, fundingFailure: false,
    emptyTicker: false, tickerPrice: 121,
    spotDelayMs: 1,
    rowCount: 180,
    gate: null as { matches: (call: Call) => boolean; promise: Promise<void> } | null,
    response: null as PublicHttpResult | null,
  };
  const metadata = (symbol: string, market: Market) => ({
    symbol, baseAsset: symbol.slice(0, -4), quoteAsset: 'USDT', status: 'TRADING', isSpotTradingAllowed: true,
    contractType: market === 'futures' ? 'PERPETUAL' : undefined, marginAsset: market === 'futures' ? 'USDT' : undefined,
    filters: [{ filterType: 'PRICE_FILTER', tickSize: '0.01' }, { filterType: 'LOT_SIZE', minQty: '0.001', stepSize: '0.001' }, { filterType: 'MIN_NOTIONAL', minNotional: '5', notional: '5' }],
  });
  const transport: PublicTransport = async (address, signal) => {
    const url = new URL(address); const market: Market = url.hostname === 'api.binance.com' ? 'spot' : 'futures';
    const call = { market, path: url.pathname, params: url.searchParams, at: state.now };
    state.calls.push(call); state.active += 1; state.maxActive = Math.max(state.maxActive, state.active);
    try {
      if (state.gate?.matches(call)) await state.gate.promise;
      await pause(market === 'spot' ? state.spotDelayMs : 1);
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      if (state.response) return state.response;
      if (state.spotFailure && market === 'spot') throw new TypeError('Failed to fetch');
      const symbols = market === 'spot' ? state.spot : state.futures;
      if (url.pathname.endsWith('/exchangeInfo')) return { status: 200, data: { symbols: [
        ...symbols.map(symbol => metadata(symbol, market)),
        { ...metadata('INACTIVEUSDT', market), status: 'BREAK' },
        { ...metadata('WRONGUSDC', market), quoteAsset: 'USDC' },
        ...(market === 'spot' ? [{ ...metadata('DISABLEDUSDT', market), isSpotTradingAllowed: false }] : [{ ...metadata('DELIVERYUSDT', market), contractType: 'CURRENT_QUARTER' }, { ...metadata('COINUSDT', market), marginAsset: 'BTC' }]),
      ] } };
      if (url.pathname.endsWith('/ticker/24hr')) {
        if (state.tickerFailure) throw new TypeError('Ticker unavailable');
        return { status: 200, data: state.emptyTicker ? [] : symbols.map(symbol => ({ symbol, lastPrice: String(state.tickerPrice), priceChangePercent: '2.1', quoteVolume: '80000000', highPrice: '124', lowPrice: '115' })) };
      }
      if (url.pathname.endsWith('/premiumIndex')) {
        if (state.fundingFailure) throw new TypeError('Funding unavailable');
        return { status: 200, data: symbols.map(symbol => ({ symbol, lastFundingRate: '.0001' })) };
      }
      if (url.pathname.endsWith('/fundingRate')) return { status: 200, data: [{ symbol: url.searchParams.get('symbol'), fundingTime: state.now - 100_000, fundingRate: '.0001', markPrice: '121' }] };
      if (url.pathname.endsWith('/klines')) {
        if (state.btcFailure && url.searchParams.get('symbol') === 'BTCUSDT') throw new TypeError('BTC unavailable');
        const interval = url.searchParams.get('interval') as Interval; const duration = INTERVAL_MS[interval];
        const closed = url.searchParams.has('endTime');
        const lastEnd = closed ? Number(url.searchParams.get('endTime')) : endOfClosed(interval, state.now) + duration;
        const count = Math.min(Number(url.searchParams.get('limit')), state.rowCount);
        const data = Array.from({ length: count }, (_, index) => {
          const end = lastEnd - (count - index - 1) * duration; const start = end - duration + 1;
          const close = !closed && index === count - 1 ? 50_000 : 100 + index * .1;
          return [start, String(close - .05), String(close + .2), String(close - .2), String(close), '10000', end];
        });
        return { status: 200, data };
      }
      throw new Error(`Unexpected endpoint: ${address}`);
    } finally { state.active -= 1; }
  };
  return { state, service: new LocalMarketService({ transport, clock: () => state.now }) };
}

test('discovers every eligible pair and scans all three channels fairly with bounded concurrency', async () => {
  const { state, service } = fixture();
  state.spot.push(...Array.from({ length: 12 }, (_, index) => `TOKEN${index}USDT`));
  state.futures.push('SOLUSDT', 'BNBUSDT');
  try {
    const spot = await service.snapshot('spot', 'swing');
    expect(spot.items.map(item => item.symbol).sort()).toEqual([...state.spot].sort());
    const futures = await service.snapshot('futures', 'intraday');
    expect(futures.items.map(item => item.symbol).sort()).toEqual([...state.futures].sort());
    await expect.poll(() => {
      const spotWeekly = new Set(state.calls.filter(call => call.market === 'spot' && call.params.get('interval') === '1w').map(call => call.params.get('symbol')));
      const futureFast = new Set(state.calls.filter(call => call.market === 'futures' && call.params.get('interval') === '3m').map(call => call.params.get('symbol')));
      return spotWeekly.size === state.spot.length && futureFast.size === state.futures.length;
    }, { timeout: 10_000 }).toBe(true);
    expect(state.maxActive).toBeLessThanOrEqual(4);
    expect((await service.snapshot('spot', 'investment')).scanProgress).toBe(100);
  } finally { service.stop(); }
});

test('metadata refresh removes a delisted coin atomically while its scan is still in flight', async () => {
  const { state, service } = fixture(); const gate = deferred();
  state.spot = ['BTCUSDT', 'OLDUSDT'];
  state.gate = { matches: call => call.market === 'spot' && call.path.endsWith('/klines') && call.params.get('symbol') === 'OLDUSDT', promise: gate.promise };
  try {
    await service.snapshot('spot', 'swing');
    await expect.poll(() => state.calls.some(call => call.params.get('symbol') === 'OLDUSDT'), { timeout: 5000 }).toBe(true);
    state.spot = ['BTCUSDT', 'NEWUSDT']; state.now += 300_001;
    const snapshot = await service.snapshot('spot', 'swing');
    expect(snapshot.items.map(item => item.symbol)).toEqual(['BTCUSDT', 'NEWUSDT']);
    gate.resolve(); state.gate = null;
    await pause(200);
    expect((await service.snapshot('spot', 'investment')).items.some(item => item.symbol === 'OLDUSDT')).toBe(false);
    await expect(service.analyzeSymbol('spot', 'OLDUSDT', 'swing')).rejects.toThrow('غير متاح');
    // Regression: completion of the removed asset must not put it back into
    // either scan coverage or the failure registry after discovery removed it.
    const failures = Reflect.get(service, 'failures') as Map<string, unknown>;
    expect([...failures.keys()].some(key => key.endsWith(':OLDUSDT'))).toBe(false);
    const channels = Reflect.get(service, 'channels') as { visited: Set<string> }[];
    expect(channels.some(channel => channel.visited.has('OLDUSDT'))).toBe(false);
  } finally { gate.resolve(); service.stop(); }
});

test('forming charts never become confirmed analysis candles across a close boundary', async () => {
  const { state, service } = fixture();
  try {
    await service.snapshot('futures', 'intraday');
    const live = await service.candles('futures', 'BTCUSDT', '3m', 180);
    expect(live[live.length - 1].close).toBe(50_000);
    const before = state.now; const expectedEnd = endOfClosed('3m', before);
    const closed = await service.closedCandles('futures', 'BTCUSDT', '3m', 180);
    expect(closed[closed.length - 1].close).not.toBe(50_000);
    expect(closed[closed.length - 1].closeTime).toBe(expectedEnd);
    state.now += 2;
    const after = await service.closedCandles('futures', 'BTCUSDT', '3m', 180);
    expect(after[after.length - 1].closeTime).toBe(expectedEnd + INTERVAL_MS['3m']);
    const requests = state.calls.filter(call => call.market === 'futures' && call.params.get('symbol') === 'BTCUSDT' && call.params.get('interval') === '3m');
    expect(requests.some(call => !call.params.has('endTime'))).toBe(true);
    expect(requests.some(call => Number(call.params.get('endTime')) === expectedEnd)).toBe(true);
    expect(requests.some(call => Number(call.params.get('endTime')) === expectedEnd + INTERVAL_MS['3m'])).toBe(true);
  } finally { service.stop(); }
});

test('stale prices and their analyses disappear after 120 seconds without a successful refresh', async () => {
  const { state, service } = fixture();
  try {
    const initial = await service.snapshot('spot', 'swing'); expect(initial.connection).toBe('live');
    await service.analyzeSymbol('spot', 'BTCUSDT', 'swing');
    state.tickerFailure = true; state.now += 120_001;
    const stale = await service.snapshot('spot', 'swing');
    expect(stale.connection).toBe('offline'); expect(stale.updatedAt).toBeNull();
    expect(stale.items.every(item => item.price === null && item.quoteVolume === null && item.analysis === null)).toBe(true);
  } finally { service.stop(); }
});

test('an empty ticker response cannot make a populated universe look live', async () => {
  const { state, service } = fixture(); state.emptyTicker = true;
  try {
    const snapshot = await service.snapshot('spot', 'swing');
    expect(snapshot.items).toHaveLength(state.spot.length);
    expect(snapshot.connection).toBe('offline'); expect(snapshot.updatedAt).toBeNull();
    expect(snapshot.items.every(item => item.price === null && item.analysis === null)).toBe(true);
    expect(snapshot.error).toContain('أسعار');
  } finally { service.stop(); }
});

test('all horizons preserve the original engine payload and explicit requests recompute with the current quote', async () => {
  const { state, service } = fixture();
  state.now = Date.UTC(2026, 9, 10, 12, 16);
  const expectedAnalysis = async (market: Market, horizon: Horizon) => {
    const intervals = REQUIRED_TIMEFRAMES[`${market}:${horizon}` as keyof typeof REQUIRED_TIMEFRAMES];
    const frames: Partial<Record<Interval, CandleWithClose[]>> = {};
    for (const interval of intervals) frames[interval] = await service.closedCandles(market, 'BTCUSDT', interval, 180);
    return analyzeCandles({
      symbol: 'BTCUSDT', market, horizon, frames,
      ticker: { price: state.tickerPrice, quoteVolume: 80_000_000 },
      btcRegime: regimeFromCandles(frames[intervals[0]]!, intervals[0], state.now),
      fundingRate: market === 'futures' ? .0001 : null, nowMs: state.now,
    });
  };
  try {
    const cases: [Market, Horizon][] = [['spot', 'swing'], ['spot', 'investment'], ['futures', 'intraday']];
    for (const [market, horizon] of cases) {
      await service.snapshot(market, horizon);
      const expected = await expectedAnalysis(market, horizon);
      const actual = await service.analyzeSymbol(market, 'BTCUSDT', horizon);
      expect(actual).toEqual(expected);
      if (market === 'spot') expect(actual.validUntil).toBeGreaterThan(actual.asOf + 120_000);

      state.tickerPrice += 3; state.now += 15_001;
      await service.snapshot(market, horizon);
      const recomputed = await service.analyzeSymbol(market, 'BTCUSDT', horizon);
      expect(recomputed).toEqual(await expectedAnalysis(market, horizon));
      expect(recomputed.asOf).toBe(state.now);
      expect(recomputed).not.toEqual(actual);
    }
  } finally { service.stop(); }
});

test('snapshot preserves a valid engine plan after price crosses its stop, target, or entry distance', async () => {
  const { state, service } = fixture();
  try {
    await service.snapshot('spot', 'swing');
    const actual = await service.analyzeSymbol('spot', 'BTCUSDT', 'swing');
    const analyses = Reflect.get(service, 'analyses') as Map<string, Analysis>;
    const plan: Analysis = { ...actual, side: 'long', entry: 121, stop: 118, targets: [126, 131, 136], atrPercent: 2, score: 85 };
    analyses.set('spot:swing:BTCUSDT', plan);
    for (const quote of [117, 126, 124]) {
      state.tickerPrice = quote; state.now += 15_001;
      const snapshot = await service.snapshot('spot', 'swing');
      const item = snapshot.items.find(value => value.symbol === 'BTCUSDT');
      expect(item?.price).toBe(quote);
      expect(item?.analysis).toEqual(plan);
    }
  } finally { service.stop(); }
});

test('a failed active BTC candle dependency propagates instead of substituting unknown context', async () => {
  const { state, service } = fixture();
  try {
    await service.snapshot('spot', 'swing');
    await service.analyzeSymbol('spot', 'ETHUSDT', 'swing');
    state.now += INTERVAL_MS['1d']; state.btcFailure = true;
    await expect(service.analyzeSymbol('spot', 'ETHUSDT', 'swing')).rejects.toThrow('Binance');
    expect(state.calls.some(call => call.market === 'spot' && call.params.get('symbol') === 'BTCUSDT' && call.at === state.now)).toBe(true);
  } finally { service.stop(); }
});

test('futures analysis refreshes funding older than ninety seconds and propagates a refresh failure', async () => {
  const { state, service } = fixture();
  try {
    await service.snapshot('futures', 'intraday');
    await service.analyzeSymbol('futures', 'BTCUSDT', 'intraday');
    state.now += 90_001; state.fundingFailure = true;
    const before = state.calls.filter(call => call.path.endsWith('/premiumIndex')).length;
    await expect(service.analyzeSymbol('futures', 'BTCUSDT', 'intraday')).rejects.toThrow('Binance');
    // The initial public refresh settles its failure; analyze_symbol then
    // requires a fresh funding dependency instead of using the old rate.
    expect(state.calls.filter(call => call.path.endsWith('/premiumIndex')).length).toBeGreaterThan(before);
  } finally { service.stop(); }
});

test('a blocked futures startup promptly rejects already-queued funding work instead of waiting for the circuit timer', async () => {
  for (const status of [451, 429]) {
    const { state, service } = fixture(); state.spotDelayMs = 20; state.response = { status, data: null, headers: { 'retry-after': '3600' } };
    try {
      const result = await Promise.race([
        service.snapshot('futures', 'intraday'),
        pause(1000).then(() => { throw new Error('Known restricted snapshot stayed pending.'); }),
      ]);
      expect(result.connection).toBe('offline'); expect(result.error).toContain(String(status));
      expect(result.items).toEqual([]); expect(service.status().queuedRequests).toBe(0);
      expect(state.calls.filter(call => call.path.endsWith('/premiumIndex'))).toHaveLength(0);
    } finally { service.stop(); }
  }
});

test('public transport preserves non-JSON restriction statuses and service honors 451/429 retry windows', async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response('<h1>restricted</h1>', { status: 451, headers: { 'Content-Type': 'text/html' } });
    const result = await publicTransport('https://api.binance.com/api/v3/exchangeInfo', new AbortController().signal);
    expect(result.status).toBe(451); expect(result.data).toBeNull();
  } finally { globalThis.fetch = originalFetch; }
  for (const status of [451, 429]) {
    const { state, service } = fixture();
    state.response = { status, data: null, headers: { 'retry-after': '60' } };
    try {
      const first = await service.snapshot('spot', 'swing'); expect(first.connection).toBe('offline'); expect(first.items).toEqual([]); expect(first.error).toContain(String(status));
      const count = state.calls.filter(call => call.market === 'spot').length;
      state.now += status === 451 ? 299_999 : 59_999;
      await service.snapshot('spot', 'swing'); await pause(150);
      expect(state.calls.filter(call => call.market === 'spot')).toHaveLength(count);
      state.response = null; state.now += 2;
      const recovered = await service.snapshot('spot', 'swing');
      expect(recovered.connection).toBe('live'); expect(recovered.items.length).toBe(state.spot.length);
    } finally { service.stop(); }
  }
});

test('network/CORS failures back off without fabricated data and recover after retry', async () => {
  const { state, service } = fixture(); state.spotFailure = true;
  try {
    const first = await service.snapshot('spot', 'swing'); expect(first.items).toEqual([]); expect(first.error).toContain('CORS');
    const count = state.calls.filter(call => call.market === 'spot').length;
    state.now += 29_999; await pause(150); await service.snapshot('spot', 'swing');
    expect(state.calls.filter(call => call.market === 'spot')).toHaveLength(count);
    state.spotFailure = false; state.now += 2;
    expect((await service.snapshot('spot', 'swing')).connection).toBe('live');
  } finally { service.stop(); }
});

test('rolling request budget stops excess work, resumes next minute, and never exceeds four HTTP calls', async () => {
  const { state, service } = fixture();
  state.rowCount = 2; state.spot = Array.from({ length: 600 }, (_, index) => `ASSET${index}USDT`);
  try {
    await service.snapshot('spot', 'swing');
    const results = Promise.allSettled(state.spot.map(symbol => service.closedCandles('spot', symbol, '4h', 180)));
    await expect.poll(() => service.status().queuedRequests, { timeout: 5000 }).toBeGreaterThan(0);
    await pause(500);
    const firstMinute = state.calls.filter(call => call.market === 'spot');
    const weight = firstMinute.reduce((total, call) => total + (call.path.endsWith('/exchangeInfo') ? 20 : call.path.endsWith('/ticker/24hr') ? 80 : 2), 0);
    expect(weight).toBeLessThanOrEqual(1100); expect(weight).toBeGreaterThan(900);
    expect(state.maxActive).toBeLessThanOrEqual(4);
    state.now += 60_001;
    const completed = await results;
    expect(completed.filter(result => result.status === 'fulfilled')).toHaveLength(600);
    expect(state.maxActive).toBeLessThanOrEqual(4);
  } finally { service.stop(); }
});

test('background pause invalidates live freshness, ignores late responses, and resumes honest polling', async () => {
  const { state, service } = fixture(); const gate = deferred();
  try {
    await service.snapshot('spot', 'swing');
    state.gate = { matches: call => call.market === 'spot' && call.path.endsWith('/klines') && !call.params.has('endTime'), promise: gate.promise };
    const request = service.candles('spot', 'ETHUSDT', '4h', 500).then(() => 'resolved', error => error instanceof DOMException ? error.name : 'error');
    await expect.poll(() => state.calls.some(call => call.market === 'spot' && call.path.endsWith('/klines') && !call.params.has('endTime'))).toBe(true);
    service.pause(); expect(service.status().paused).toBe(true);
    const count = state.calls.length; state.now += 1000; await pause(200);
    expect(state.calls).toHaveLength(count);
    await expect(service.snapshot('spot', 'swing')).rejects.toThrow('الخلفية');
    gate.resolve(); state.gate = null; expect(await request).toBe('AbortError');
    service.resume(); const resumed = await service.snapshot('spot', 'swing');
    expect(resumed.connection).toBe('live'); expect(resumed.updatedAt).toBe(state.now); expect(service.status().paused).toBe(false);
  } finally { gate.resolve(); service.stop(); }
});
