import { Capacitor, CapacitorHttp } from '@capacitor/core';
import { analyzeCandles, AnalysisUnavailable, INTERVAL_MS, REQUIRED_TIMEFRAMES, regimeFromCandles } from './local-analysis';
import type { CandleWithClose, Interval } from './local-analysis';
import type { Analysis, Horizon, Market, MarketItem, MarketSnapshot } from './types';

const ENDPOINTS: Record<Market, string> = { spot: 'https://api.binance.com', futures: 'https://fapi.binance.com' };
const HORIZONS: Record<Market, Horizon[]> = { spot: ['swing', 'investment'], futures: ['intraday'] };
const CHART_INTERVALS: Record<Market, string[]> = { spot: ['4h', '1d', '1w'], futures: ['3m', '5m', '15m', '30m', '1h'] };
const UNIVERSE_TTL = 300_000;
const UNIVERSE_MAX_AGE = 3_600_000;
const TICKER_TTL = 15_000;
const TICKER_MAX_AGE = 120_000;
const FUNDING_TTL = 45_000;
const MAX_CACHE_CANDLES = 450_000;
const ANALYSIS_REFRESH: Record<Market, number> = { spot: 60_000, futures: 30_000 };
type Metadata = Pick<MarketItem, 'symbol' | 'baseAsset' | 'quoteAsset' | 'pricePrecision' | 'quantityPrecision' | 'tickSize' | 'stepSize' | 'minQty' | 'minNotional'>;
type Ticker = Pick<MarketItem, 'price' | 'change24h' | 'quoteVolume' | 'high24h' | 'low24h'>;
type Resource = 'universe' | 'ticker' | 'funding';
type Priority = 0 | 1 | 2;
export interface HistoricalFunding { fundingTime: number; fundingRate: number; markPrice?: number }
export interface PublicHttpResult { status: number; data: unknown; headers?: Record<string, string> }
export type PublicTransport = (url: string, signal: AbortSignal) => Promise<PublicHttpResult>;

export class LocalMarketError extends Error {
  constructor(message: string, public status = 0) { super(message); this.name = 'LocalMarketError'; }
}

function record(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value); }
function finite(value: unknown): number { const result = Number(value); if ((typeof value !== 'string' && typeof value !== 'number') || (typeof value === 'string' && !value.trim()) || !Number.isFinite(result)) throw new LocalMarketError('مصدر Binance أعاد قيمة غير صالحة.'); return result; }
function positive(value: unknown, zero = false): number { const result = finite(value); if (zero ? result < 0 : result <= 0) throw new LocalMarketError('مصدر Binance أعاد قيمة غير صالحة.'); return result; }
function decimals(value: number, fallback = 8): number {
  if (!Number.isFinite(value) || value <= 0) return fallback;
  const [coefficient, exponent = '0'] = value.toString().toLowerCase().split('e');
  return Math.max(0, (coefficient.split('.')[1]?.length ?? 0) - Number(exponent));
}
function abortError() { return new DOMException('تم إلغاء الطلب.', 'AbortError'); }
function checkAbort(signal?: AbortSignal) { if (signal?.aborted) throw abortError(); }
function isAbort(error: unknown) { return error instanceof DOMException && error.name === 'AbortError'; }
function message(error: unknown) { return error instanceof Error ? error.message : 'تعذر الاتصال بمصدر Binance.'; }
function withAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) { void promise.catch(() => undefined); return Promise.reject(abortError()); }
  return new Promise<T>((resolve, reject) => {
    const abort = () => { cleanup(); reject(abortError()); };
    const cleanup = () => signal.removeEventListener('abort', abort);
    signal.addEventListener('abort', abort, { once: true });
    promise.then(value => { cleanup(); if (signal.aborted) reject(abortError()); else resolve(value); }, error => { cleanup(); reject(error); });
  });
}

/** Native HTTP avoids WKWebView CORS; web uses the exchange's public HTTPS endpoints directly. */
export const publicTransport: PublicTransport = async (url, signal) => {
  checkAbort(signal);
  if (Capacitor.isNativePlatform()) {
    // Core native HTTP has no cancellation API. Keep the scheduler slot until
    // it actually settles; ignore every response from an aborted generation.
    const response = await CapacitorHttp.get({ url, headers: { Accept: 'application/json' }, responseType: 'json', connectTimeout: 10_000, readTimeout: 15_000, disableRedirects: true });
    checkAbort(signal);
    let data: unknown = response.data;
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch { if (response.status >= 200 && response.status < 300) throw new LocalMarketError('مصدر Binance أعاد بيانات غير صالحة.'); data = null; } }
    return { status: response.status, data, headers: response.headers };
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 20_000);
  try {
    const response = await fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' }, credentials: 'omit', cache: 'no-store', redirect: 'error' });
    const data: unknown = await response.json().catch(() => null);
    checkAbort(signal);
    if (data === null && response.ok) throw new LocalMarketError('مصدر Binance أعاد بيانات غير صالحة.');
    return { status: response.status, data, headers: Object.fromEntries(response.headers.entries()) };
  } catch (error) {
    if (signal.aborted) throw abortError();
    if (timedOut) throw new LocalMarketError('انتهت مهلة الاتصال المباشر بـ Binance.');
    if (error instanceof LocalMarketError) throw error;
    throw new LocalMarketError('تعذر الاتصال المباشر بـ Binance؛ تحقق من الشبكة. قد تمنع قيود المنطقة أو CORS اتصال المتصفح.');
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
};

class RequestBudget {
  private events: { at: number; weight: number }[] = [];
  private used = 0;
  pauseUntil = 0;
  constructor(readonly capacity: number, readonly windowMs = 60_000) {}
  available(weight: number, now: number) {
    while (this.events.length && this.events[0].at + this.windowMs <= now) this.used -= this.events.shift()!.weight;
    return now >= this.pauseUntil && this.used + weight <= this.capacity;
  }
  reserve(weight: number, now: number) { this.events.push({ at: now, weight }); this.used += weight; }
  observe(headers: Record<string, string> | undefined, now: number) {
    const pair = Object.entries(headers ?? {}).find(([key]) => key.toLowerCase() === 'x-mbx-used-weight-1m');
    if (pair && Number(pair[1]) >= this.capacity) this.pauseUntil = Math.max(this.pauseUntil, now + 60_000);
  }
}

interface MarketState {
  metadata: Map<string, Metadata>;
  symbols: string[];
  universeAt: number;
  tickers: Map<string, Ticker>;
  tickerAt: number;
  funding: Map<string, number>;
  fundingAt: number;
  errors: Partial<Record<Resource, string>>;
  retryAt: Partial<Record<Resource, number>>;
  blockedUntil: number;
  blockedMessage: string | null;
}
interface CacheEntry { rows: CandleWithClose[]; limit: number; expiresAt: number; touchedAt: number }
interface RequestJob {
  market: Market; path: string; params: Record<string, string>; weight: number; priority: Priority;
  signal: AbortSignal; sequence: number; resolve: (value: unknown) => void; reject: (error: unknown) => void;
}
interface ScanChannel { market: Market; horizon: Horizon; cursor: number; visited: Set<string>; busy: boolean }

function freshState(): MarketState {
  return { metadata: new Map(), symbols: [], universeAt: 0, tickers: new Map(), tickerAt: 0, funding: new Map(), fundingAt: 0, errors: {}, retryAt: {}, blockedUntil: 0, blockedMessage: null };
}

/** Every active USDT pair belongs to the universe. Memory/request limits affect caching and scan speed, never eligibility. */
export class LocalMarketService {
  private readonly transport: PublicTransport;
  private readonly clock: () => number;
  private readonly states: Record<Market, MarketState> = { spot: freshState(), futures: freshState() };
  private readonly budgets: Record<Market, RequestBudget> = { spot: new RequestBudget(1100), futures: new RequestBudget(2000) };
  private readonly fundingBudget = new RequestBudget(400, 300_000);
  private readonly cache = new Map<string, CacheEntry>();
  private readonly locks = new Map<string, Promise<unknown>>();
  private readonly analyses = new Map<string, Analysis>();
  private readonly failures = new Map<string, { at: number; count: number }>();
  private readonly channels: ScanChannel[] = [
    { market: 'spot', horizon: 'swing', cursor: 0, visited: new Set(), busy: false },
    { market: 'spot', horizon: 'investment', cursor: 0, visited: new Set(), busy: false },
    { market: 'futures', horizon: 'intraday', cursor: 0, visited: new Set(), busy: false },
  ];
  private queue: RequestJob[] = [];
  private inFlight = 0;
  private sequence = 0;
  private turn: Market = 'spot';
  private running = false;
  private paused = false;
  private generation = 0;
  private controller = new AbortController();
  private timer: ReturnType<typeof setInterval> | null = null;
  private dispatchTimer: ReturnType<typeof setTimeout> | null = null;
  private startedAt = 0;
  private readonly onVisibility = () => { if (document.visibilityState === 'hidden') this.pause(); else this.resume(); };

  constructor(options: { transport?: PublicTransport; clock?: () => number } = {}) {
    this.transport = options.transport ?? publicTransport;
    this.clock = options.clock ?? Date.now;
  }

  start(): void {
    if (this.running) return;
    this.running = true; this.paused = typeof document !== 'undefined' && document.visibilityState === 'hidden';
    this.generation += 1; this.controller = new AbortController(); this.startedAt = this.clock();
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', this.onVisibility);
    this.timer = setInterval(() => this.tick(), 125);
    this.tick();
  }
  stop(): void {
    this.running = false; this.paused = false; this.invalidate();
    if (this.timer) clearInterval(this.timer); this.timer = null;
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', this.onVisibility);
  }
  pause(): void {
    if (!this.running || this.paused) return;
    this.paused = true; this.invalidate();
    // A suspended phone cannot claim that its last poll is still a live feed.
    this.states.spot.tickerAt = 0; this.states.futures.tickerAt = 0;
  }
  resume(): void {
    if (!this.running) { this.start(); return; }
    if (!this.paused) return;
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    this.paused = false; this.generation += 1; this.controller = new AbortController(); this.tick();
  }
  private invalidate() {
    this.generation += 1; this.controller.abort();
    for (const job of this.queue) job.reject(abortError()); this.queue = [];
    if (this.dispatchTimer) clearTimeout(this.dispatchTimer); this.dispatchTimer = null;
    this.locks.clear();
    for (const channel of this.channels) channel.busy = false;
  }
  private assertCurrent(generation: number) { if (!this.running || this.paused || generation !== this.generation || this.controller.signal.aborted) throw abortError(); }
  private key(market: Market, horizon: Horizon, symbol: string) { return `${market}:${horizon}:${symbol}`; }
  private validate(market: Market, horizon?: Horizon) {
    if (!(market in ENDPOINTS) || (horizon && !HORIZONS[market].includes(horizon))) throw new LocalMarketError('قسم أو أسلوب تحليل غير صالح.');
  }
  private active(market: Market, symbol: string) { return this.states[market].metadata.has(symbol) && this.clock() - this.states[market].universeAt <= UNIVERSE_MAX_AGE; }
  private locked<T>(key: string, load: () => Promise<T>): Promise<T> {
    const existing = this.locks.get(key); if (existing) return existing as Promise<T>;
    const promise = load().finally(() => { if (this.locks.get(key) === promise) this.locks.delete(key); });
    this.locks.set(key, promise); return promise;
  }
  private request(market: Market, path: string, params: Record<string, string>, weight: number, priority: Priority): Promise<unknown> {
    if (!this.running || this.paused || this.controller.signal.aborted) return Promise.reject(abortError());
    const state = this.states[market];
    if (state.blockedUntil > this.clock()) return Promise.reject(new LocalMarketError(state.blockedMessage ?? 'مصدر Binance غير متاح الآن.'));
    const promise = new Promise<unknown>((resolve, reject) => this.queue.push({ market, path, params, weight, priority, signal: this.controller.signal, sequence: this.sequence++, resolve, reject }));
    this.dispatch(); return promise;
  }
  private dispatch(): void {
    if (this.dispatchTimer) { clearTimeout(this.dispatchTimer); this.dispatchTimer = null; }
    if (!this.running || this.paused) return;
    this.queue = this.queue.filter(job => {
      if (job.signal.aborted) { job.reject(abortError()); return false; }
      const state = this.states[job.market];
      if (state.blockedUntil > this.clock()) { job.reject(new LocalMarketError(state.blockedMessage ?? 'مصدر Binance غير متاح الآن.')); return false; }
      return true;
    });
    while (this.inFlight < 4 && this.queue.length) {
      const now = this.clock();
      const eligible = this.queue.filter(job => this.budgets[job.market].available(job.weight, now) && (job.path !== '/fapi/v1/fundingRate' || this.fundingBudget.available(1, now)));
      eligible.sort((a, b) => a.priority - b.priority || Number(b.market === this.turn) - Number(a.market === this.turn) || a.sequence - b.sequence);
      const job = eligible[0]; if (!job) break;
      this.queue.splice(this.queue.indexOf(job), 1);
      if (this.states[job.market].blockedUntil > now) { job.reject(new LocalMarketError(this.states[job.market].blockedMessage ?? 'مصدر Binance غير متاح الآن.')); continue; }
      this.turn = job.market === 'spot' ? 'futures' : 'spot';
      this.budgets[job.market].reserve(job.weight, now);
      if (job.path === '/fapi/v1/fundingRate') this.fundingBudget.reserve(1, now);
      this.inFlight += 1;
      void this.performRequest(job).then(job.resolve, job.reject).finally(() => { this.inFlight -= 1; this.dispatch(); });
    }
    if (this.queue.length && !this.dispatchTimer) this.dispatchTimer = setTimeout(() => { this.dispatchTimer = null; this.dispatch(); }, 250);
  }
  private async performRequest(job: RequestJob): Promise<unknown> {
    checkAbort(job.signal);
    const query = new URLSearchParams(job.params).toString();
    let response: PublicHttpResult;
    try { response = await this.transport(`${ENDPOINTS[job.market]}${job.path}${query ? `?${query}` : ''}`, job.signal); }
    catch (error) {
      if (job.signal.aborted || isAbort(error)) throw abortError();
      if (error instanceof LocalMarketError) throw error;
      throw new LocalMarketError(Capacitor.isNativePlatform() ? 'تعذر اتصال الهاتف بـ Binance. تحقق من الشبكة وتوفر المنصة في منطقتك.' : 'تعذر الاتصال بـ Binance؛ قد تمنع الشبكة أو قيود CORS طلب المتصفح.');
    }
    checkAbort(job.signal);
    const now = this.clock(); const state = this.states[job.market];
    this.budgets[job.market].observe(response.headers, now);
    if ([403, 451, 418, 429].includes(response.status)) {
      const limited = response.status === 418 || response.status === 429;
      const retry = Object.entries(response.headers ?? {}).find(([key]) => key.toLowerCase() === 'retry-after')?.[1];
      const delay = limited ? Math.min(3_600_000, Math.max(60_000, (Number(retry) || 60) * 1000)) : 300_000;
      const text = limited ? `Binance أوقف الطلبات مؤقتًا بسبب حد البيانات (${response.status}).` : `Binance غير متاح من شبكة هذا الجهاز (${response.status}).`;
      state.blockedUntil = now + delay; state.blockedMessage = text;
      this.budgets[job.market].pauseUntil = state.blockedUntil;
      throw new LocalMarketError(text, response.status);
    }
    if (record(response.data) && Number(response.data.code) < 0) {
      if (Number(response.data.code) === -1121 && job.params.symbol) this.removeSymbol(job.market, job.params.symbol);
      throw new LocalMarketError('Binance رفض طلب البيانات أو أن الزوج لم يعد متاحًا.', response.status);
    }
    if (response.status < 200 || response.status >= 300) throw new LocalMarketError(`تعذر جلب البيانات المباشرة من Binance (${response.status}).`, response.status);
    return response.data;
  }

  private parseMetadata(value: unknown, market: Market): Metadata | null {
    if (!record(value) || value.status !== 'TRADING' || value.quoteAsset !== 'USDT' || typeof value.symbol !== 'string' || typeof value.baseAsset !== 'string') return null;
    if (market === 'spot' && value.isSpotTradingAllowed === false) return null;
    if (market === 'futures' && (value.contractType !== 'PERPETUAL' || value.marginAsset !== 'USDT')) return null;
    if (!Array.isArray(value.filters)) return null;
    const filters = new Map(value.filters.filter(record).map(filter => [String(filter.filterType), filter]));
    const priceFilter = filters.get('PRICE_FILTER'); const lot = filters.get('LOT_SIZE');
    const notional = filters.get('NOTIONAL') ?? filters.get('MIN_NOTIONAL');
    try {
      const tickSize = positive(priceFilter?.tickSize, true); const stepSize = positive(lot?.stepSize, true);
      return { symbol: value.symbol, baseAsset: value.baseAsset, quoteAsset: 'USDT', tickSize, stepSize, pricePrecision: decimals(tickSize, Number(value.pricePrecision ?? value.quotePrecision ?? 8)), quantityPrecision: decimals(stepSize, Number(value.quantityPrecision ?? value.baseAssetPrecision ?? 8)), minQty: positive(lot?.minQty, true), minNotional: positive(notional?.minNotional ?? notional?.notional ?? '0', true) };
    } catch { return null; }
  }
  private removeSymbol(market: Market, symbol: string) {
    const state = this.states[market]; state.metadata.delete(symbol); state.symbols = state.symbols.filter(value => value !== symbol); state.tickers.delete(symbol); state.funding.delete(symbol);
    for (const horizon of HORIZONS[market]) { this.analyses.delete(this.key(market, horizon, symbol)); this.failures.delete(this.key(market, horizon, symbol)); }
    for (const key of this.cache.keys()) if (key.startsWith(`${market}:${symbol}:`)) this.cache.delete(key);
    for (const channel of this.channels) if (channel.market === market) { channel.visited.delete(symbol); channel.cursor = Math.min(channel.cursor, state.symbols.length); }
  }
  private resource<T>(market: Market, resource: Resource, load: () => Promise<T>): Promise<T> {
    const state = this.states[market]; const generation = this.generation;
    return this.locked(`${generation}:${market}:${resource}`, async () => {
      try { const value = await load(); this.assertCurrent(generation); delete state.errors[resource]; delete state.retryAt[resource]; return value; }
      catch (error) { if (!isAbort(error) && generation === this.generation) { state.errors[resource] = message(error); state.retryAt[resource] = this.clock() + 30_000; } throw error; }
    });
  }
  private async refreshUniverse(market: Market, force = false): Promise<void> {
    const state = this.states[market]; const generation = this.generation;
    if (!force && state.universeAt && this.clock() - state.universeAt < UNIVERSE_TTL) return;
    await this.resource(market, 'universe', async () => {
      const raw = await this.request(market, market === 'spot' ? '/api/v3/exchangeInfo' : '/fapi/v1/exchangeInfo', {}, market === 'spot' ? 20 : 1, 1);
      this.assertCurrent(generation);
      if (!record(raw) || !Array.isArray(raw.symbols)) throw new LocalMarketError('قائمة أزواج Binance غير صالحة.');
      const metadata = raw.symbols.map(value => this.parseMetadata(value, market)).filter((value): value is Metadata => !!value);
      const next = new Map(metadata.map(value => [value.symbol, value]));
      for (const symbol of state.metadata.keys()) if (!next.has(symbol)) this.removeSymbol(market, symbol);
      state.metadata = next; state.symbols = [...next.keys()].sort((a, b) => Number(b === 'BTCUSDT') - Number(a === 'BTCUSDT') || a.localeCompare(b)); state.universeAt = this.clock();
      state.tickers = new Map([...state.tickers].filter(([symbol]) => next.has(symbol)));
      state.funding = new Map([...state.funding].filter(([symbol]) => next.has(symbol)));
      for (const channel of this.channels) if (channel.market === market) { channel.cursor = Math.min(channel.cursor, state.symbols.length); channel.visited = new Set([...channel.visited].filter(symbol => next.has(symbol))); }
    });
  }
  private async refreshTickers(market: Market, priority: Priority = 1): Promise<void> {
    const state = this.states[market]; const generation = this.generation;
    if (state.tickerAt && this.clock() - state.tickerAt < TICKER_TTL) return;
    await this.resource(market, 'ticker', async () => {
      const raw = await this.request(market, market === 'spot' ? '/api/v3/ticker/24hr' : '/fapi/v1/ticker/24hr', {}, market === 'spot' ? 80 : 40, priority);
      this.assertCurrent(generation);
      if (!Array.isArray(raw) || !raw.length) throw new LocalMarketError('لم تصل أسعار USDT صحيحة من Binance.');
      const tickers = new Map<string, Ticker>();
      for (const row of raw) {
        if (!record(row) || typeof row.symbol !== 'string' || !row.symbol.endsWith('USDT') || (state.universeAt && !state.metadata.has(row.symbol))) continue;
        try { tickers.set(row.symbol, { price: positive(row.lastPrice), change24h: finite(row.priceChangePercent), quoteVolume: positive(row.quoteVolume, true), high24h: positive(row.highPrice), low24h: positive(row.lowPrice) }); } catch { /* Invalid ticker never becomes zero or a fabricated price. */ }
      }
      if (raw.length && !tickers.size) throw new LocalMarketError('لم تصل أسعار USDT صحيحة من Binance.');
      state.tickers = tickers; state.tickerAt = this.clock();
    });
  }
  private async refreshFunding(priority: Priority = 1): Promise<void> {
    const state = this.states.futures; const generation = this.generation;
    if (state.fundingAt && this.clock() - state.fundingAt < FUNDING_TTL) return;
    await this.resource('futures', 'funding', async () => {
      const raw = await this.request('futures', '/fapi/v1/premiumIndex', {}, 10, priority);
      this.assertCurrent(generation);
      if (!Array.isArray(raw)) throw new LocalMarketError('بيانات التمويل من Binance غير صالحة.');
      const funding = new Map<string, number>();
      for (const row of raw) if (record(row) && typeof row.symbol === 'string') { try { funding.set(row.symbol, finite(row.lastFundingRate)); } catch { /* Unknown funding stays unknown. */ } }
      state.funding = funding; state.fundingAt = this.clock();
    });
  }
  private async ensure(market: Market) {
    this.start(); if (this.paused) throw new LocalMarketError('تحديث السوق متوقف أثناء وجود التطبيق في الخلفية.');
    const generation = this.generation; const state = this.states[market]; const now = this.clock();
    if (state.blockedUntil > now) return;
    const work: Promise<unknown>[] = [];
    if ((!state.universeAt || now - state.universeAt >= UNIVERSE_TTL) && now >= (state.retryAt.universe ?? 0)) work.push(this.refreshUniverse(market));
    if ((!state.tickerAt || now - state.tickerAt >= TICKER_TTL) && now >= (state.retryAt.ticker ?? 0)) work.push(this.refreshTickers(market));
    if (market === 'futures' && (!state.fundingAt || now - state.fundingAt >= FUNDING_TTL) && now >= (state.retryAt.funding ?? 0)) work.push(this.refreshFunding());
    await Promise.allSettled(work);
    this.assertCurrent(generation);
  }
  private closedEnd(interval: Interval, now: number) {
    const offset = interval === '1w' ? 4 * INTERVAL_MS['1d'] : 0;
    return Math.floor((now - offset) / INTERVAL_MS[interval]) * INTERVAL_MS[interval] + offset - 1;
  }
  private parseCandles(raw: unknown, interval: Interval, closedEnd?: number): CandleWithClose[] {
    if (!Array.isArray(raw)) throw new LocalMarketError('شموع Binance غير صالحة.');
    const seen = new Set<number>();
    const rows = raw.map(value => {
      if (!Array.isArray(value) || value.length < 7) throw new LocalMarketError('شموع Binance غير صالحة.');
      const start = finite(value[0]); const closeTime = finite(value[6]);
      const candle = { time: start / 1000, open: positive(value[1]), high: positive(value[2]), low: positive(value[3]), close: positive(value[4]), volume: positive(value[5], true), closeTime };
      if (!Number.isInteger(start) || start % 1000 || closeTime !== start + INTERVAL_MS[interval] - 1 || seen.has(start) || candle.high < Math.max(candle.open, candle.close, candle.low) || candle.low > Math.min(candle.open, candle.close, candle.high)) throw new LocalMarketError('مصدر Binance أعاد شموعًا غير متسقة.');
      seen.add(start); return candle;
    });
    return rows.filter(row => closedEnd === undefined || row.closeTime <= closedEnd).sort((a, b) => a.time - b.time);
  }
  private trimCache() {
    let count = 0; for (const value of this.cache.values()) count += value.rows.length;
    if (count <= MAX_CACHE_CANDLES) return;
    const entries = [...this.cache].sort((a, b) => a[1].touchedAt - b[1].touchedAt);
    for (const [key, value] of entries) { this.cache.delete(key); count -= value.rows.length; if (count <= MAX_CACHE_CANDLES * .9) break; }
  }
  private loadCandles(market: Market, symbol: string, interval: Interval, limit: number, closed: boolean, priority: Priority): Promise<CandleWithClose[]> {
    const generation = this.generation; const now = this.clock(); const key = `${market}:${symbol}:${interval}:${closed ? 'closed' : 'live'}`;
    const cached = this.cache.get(key);
    if (cached && cached.limit >= limit && cached.expiresAt > now) { cached.touchedAt = now; return Promise.resolve(cached.rows.slice(-limit)); }
    return this.locked(`${generation}:${key}:${limit}`, async () => {
      if (!this.active(market, symbol)) throw new LocalMarketError('الزوج غير متاح في قائمة Binance الحالية.');
      const started = this.clock(); const end = closed ? this.closedEnd(interval, started) : undefined;
      const params: Record<string, string> = { symbol, interval, limit: String(limit) }; if (end !== undefined) params.endTime = String(end);
      const weight = market === 'spot' ? 2 : limit < 100 ? 1 : limit < 500 ? 2 : limit <= 1000 ? 5 : 10;
      const raw = await this.request(market, market === 'spot' ? '/api/v3/klines' : '/fapi/v1/klines', params, weight, priority);
      this.assertCurrent(generation);
      const rows = this.parseCandles(raw, interval, end);
      if (!rows.length) throw new LocalMarketError('لا توجد شموع كافية لهذا الزوج.');
      if (!this.active(market, symbol)) throw new LocalMarketError('الزوج أزيل من Binance أثناء جلب الشموع.');
      this.cache.set(key, { rows, limit, touchedAt: this.clock(), expiresAt: closed && end !== undefined ? end + INTERVAL_MS[interval] + 1 : this.clock() + 10_000 });
      this.trimCache(); return rows.slice(-limit);
    });
  }
  private btcRegime(market: Market, horizon: Horizon): string {
    const interval: Interval = market === 'futures' ? '1h' : horizon === 'investment' ? '1w' : '1d';
    const cache = this.cache.get(`${market}:BTCUSDT:${interval}:closed`);
    if (!cache || cache.expiresAt <= this.clock()) return 'unknown';
    try { return regimeFromCandles(cache.rows, interval, this.clock()); } catch { return 'unknown'; }
  }
  private usable(analysis: Analysis | undefined, ticker: Ticker | undefined, now = this.clock()): analysis is Analysis {
    return !!analysis && analysis.validUntil > now && !!ticker;
  }
  private analyze(market: Market, symbol: string, horizon: Horizon, priority: Priority): Promise<Analysis> {
    const generation = this.generation; const key = this.key(market, horizon, symbol);
    return this.locked(`${generation}:analysis:${key}`, async () => {
      if (!this.active(market, symbol)) throw new LocalMarketError('الزوج غير متاح في Binance.');
      const state = this.states[market];
      if (!state.tickerAt || this.clock() - state.tickerAt > 30_000) await this.refreshTickers(market, priority);
      const currentTicker = state.tickers.get(symbol);
      if (!currentTicker || typeof currentTicker.price !== 'number' || typeof currentTicker.quoteVolume !== 'number' || !state.tickerAt || this.clock() - state.tickerAt > TICKER_MAX_AGE) throw new AnalysisUnavailable('السعر والسيولة الحالية غير متاحين.');
      const required = REQUIRED_TIMEFRAMES[`${market}:${horizon}` as keyof typeof REQUIRED_TIMEFRAMES];
      if (!required) throw new LocalMarketError('أسلوب التحليل غير صالح.');
      const rows = await Promise.all(required.map(interval => this.loadCandles(market, symbol, interval, 180, true, priority)));
      if (symbol !== 'BTCUSDT' && this.active(market, 'BTCUSDT')) await this.loadCandles(market, 'BTCUSDT', required[0], 180, true, priority);
      if (market === 'futures' && this.clock() - state.fundingAt > 90_000) await this.refreshFunding(priority);
      this.assertCurrent(generation);
      const ticker = state.tickers.get(symbol);
      if (!ticker || typeof ticker.price !== 'number' || typeof ticker.quoteVolume !== 'number' || this.clock() - state.tickerAt > TICKER_MAX_AGE) throw new AnalysisUnavailable('السعر والسيولة الحالية غير متاحين.');
      const frames: Partial<Record<Interval, CandleWithClose[]>> = {}; required.forEach((interval, i) => { frames[interval] = rows[i]; });
      // Yield before bounded indicator work so chart gestures and navigation
      // remain responsive even when several network replies arrive together.
      await new Promise<void>(resolve => setTimeout(resolve, 0)); this.assertCurrent(generation);
      const analysis = analyzeCandles({ symbol, market, horizon, frames, ticker: { price: ticker.price, quoteVolume: ticker.quoteVolume }, btcRegime: this.btcRegime(market, horizon), fundingRate: market === 'futures' && this.clock() - state.fundingAt <= TICKER_MAX_AGE ? state.funding.get(symbol) ?? null : null, nowMs: this.clock() });
      if (!this.active(market, symbol)) throw new LocalMarketError('الزوج أزيل من Binance أثناء التحليل.');
      this.analyses.set(key, analysis); this.failures.delete(key); return analysis;
    });
  }

  private tick(): void {
    if (!this.running || this.paused) return;
    const now = this.clock();
    for (const market of ['spot', 'futures'] as const) {
      const state = this.states[market]; if (state.blockedUntil > now) continue;
      if ((!state.universeAt || now - state.universeAt >= UNIVERSE_TTL) && now >= (state.retryAt.universe ?? 0)) void this.refreshUniverse(market).catch(() => undefined);
      if ((!state.tickerAt || now - state.tickerAt >= TICKER_TTL) && now >= (state.retryAt.ticker ?? 0)) void this.refreshTickers(market).catch(() => undefined);
      if (market === 'futures' && (!state.fundingAt || now - state.fundingAt >= FUNDING_TTL) && now >= (state.retryAt.funding ?? 0)) void this.refreshFunding().catch(() => undefined);
    }
    for (const channel of this.channels) {
      const state = this.states[channel.market]; if (channel.busy || !state.symbols.length || !state.tickerAt || now - state.universeAt > UNIVERSE_MAX_AGE || now - state.tickerAt > TICKER_MAX_AGE || state.blockedUntil > now) continue;
      let symbol: string | null = null;
      // Each channel has its own round-robin cursor. No asset is removed from
      // consideration because it has low volume, low score or a high rank.
      for (let checked = 0; checked < state.symbols.length; checked++) {
        const candidate = state.symbols[channel.cursor % state.symbols.length]; channel.cursor = (channel.cursor + 1) % state.symbols.length;
        const key = this.key(channel.market, channel.horizon, candidate); const analysis = this.analyses.get(key); const failure = this.failures.get(key);
        if (!state.tickers.has(candidate)) { channel.visited.add(candidate); continue; }
        if (this.usable(analysis, state.tickers.get(candidate), now) && now - analysis.asOf < ANALYSIS_REFRESH[channel.market]) { channel.visited.add(candidate); continue; }
        if (failure && failure.at > now) continue;
        symbol = candidate; break;
      }
      if (!symbol) continue;
      channel.busy = true; const generation = this.generation; const key = this.key(channel.market, channel.horizon, symbol); const selected = symbol;
      void this.analyze(channel.market, selected, channel.horizon, 2).catch(error => {
        if (!isAbort(error) && generation === this.generation && this.states[channel.market].metadata.has(selected)) { this.analyses.delete(key); const count = (this.failures.get(key)?.count ?? 0) + 1; this.failures.set(key, { count, at: this.clock() + Math.min(300_000, 30_000 * 2 ** Math.min(3, count - 1)) }); }
      }).finally(() => { if (generation === this.generation) { channel.busy = false; if (this.states[channel.market].metadata.has(selected)) channel.visited.add(selected); } });
    }
    this.dispatch();
  }

  async snapshot(market: Market, horizon: Horizon, signal?: AbortSignal): Promise<MarketSnapshot> {
    this.validate(market, horizon); checkAbort(signal);
    await withAbort(this.ensure(market), signal); checkAbort(signal);
    const now = this.clock(); const state = this.states[market];
    const freshUniverse = !!state.universeAt && now - state.universeAt <= UNIVERSE_MAX_AGE;
    const freshTicker = !!state.tickerAt && now - state.tickerAt <= TICKER_MAX_AGE && state.symbols.some(symbol => state.tickers.has(symbol));
    const items: MarketItem[] = freshUniverse ? state.symbols.map(symbol => {
      const metadata = state.metadata.get(symbol)!; const ticker = freshTicker ? state.tickers.get(symbol) : undefined;
      const analysis = this.analyses.get(this.key(market, horizon, symbol));
      return { ...metadata, ...(ticker ?? { price: null, change24h: null, quoteVolume: null, high24h: null, low24h: null }), analysis: this.usable(analysis, ticker, now) ? analysis : null, ...(market === 'futures' ? { fundingRate: state.fundingAt && now - state.fundingAt <= TICKER_MAX_AGE ? state.funding.get(symbol) ?? null : null } : {}) };
    }) : [];
    const error = state.blockedUntil > now ? state.blockedMessage : state.errors.universe ?? state.errors.ticker ?? state.errors.funding ?? (!freshTicker || !freshUniverse ? 'بانتظار اتصال الجهاز ببيانات Binance الحقيقية.' : null);
    const channel = this.channels.find(value => value.market === market && value.horizon === horizon)!;
    return { market, horizon, updatedAt: freshTicker ? state.tickerAt : null, source: 'binance', connection: items.length && freshTicker ? error ? 'degraded' : 'live' : 'offline', totalSymbols: items.length, analyzedSymbols: items.filter(item => !!item.analysis).length, scanProgress: items.length ? Math.min(100, Math.round(channel.visited.size / items.length * 1000) / 10) : 0, btcRegime: this.btcRegime(market, horizon), items, error };
  }
  async candles(market: Market, symbol: string, interval: string, limit = 500, signal?: AbortSignal): Promise<CandleWithClose[]> {
    this.validate(market); checkAbort(signal);
    if (!CHART_INTERVALS[market].includes(interval) || !Number.isInteger(limit) || limit < 2 || limit > 1000) throw new LocalMarketError('إطار الشارت أو عدد الشموع غير صالح.');
    await withAbort(this.ensure(market), signal);
    return withAbort(this.loadCandles(market, symbol, interval as Interval, limit, false, 0), signal);
  }
  async closedCandles(market: Market, symbol: string, interval: Interval, limit = 1000, signal?: AbortSignal): Promise<CandleWithClose[]> {
    this.validate(market); checkAbort(signal);
    if (!(interval in INTERVAL_MS) || !Number.isInteger(limit) || limit < 2 || limit > 1000) throw new LocalMarketError('إطار الاختبار أو عدد الشموع غير صالح.');
    await withAbort(this.ensure(market), signal);
    return withAbort(this.loadCandles(market, symbol, interval, limit, true, 0), signal);
  }
  async analyzeSymbol(market: Market, symbol: string, horizon: Horizon, signal?: AbortSignal): Promise<Analysis> {
    this.validate(market, horizon); checkAbort(signal); await withAbort(this.ensure(market), signal);
    return withAbort(this.analyze(market, symbol, horizon, 0), signal);
  }
  async historicalFunding(symbol: string, limit = 1000, signal?: AbortSignal): Promise<HistoricalFunding[]> {
    checkAbort(signal); if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw new LocalMarketError('عدد سجلات التمويل غير صالح.');
    await withAbort(this.ensure('futures'), signal); if (!this.active('futures', symbol)) throw new LocalMarketError('الزوج غير متاح في فيوتشر Binance.');
    const generation = this.generation;
    const raw = await withAbort(this.request('futures', '/fapi/v1/fundingRate', { symbol, limit: String(limit) }, 1, 0), signal); this.assertCurrent(generation);
    if (!this.active('futures', symbol)) throw new LocalMarketError('الزوج أزيل من Binance أثناء جلب سجل التمويل.');
    if (!Array.isArray(raw)) throw new LocalMarketError('سجل التمويل غير صالح.');
    const rows = raw.map(value => {
      if (!record(value) || (value.symbol !== undefined && value.symbol !== symbol)) throw new LocalMarketError('سجل التمويل غير صالح.');
      const fundingTime = positive(value.fundingTime); const fundingRate = finite(value.fundingRate);
      return { fundingTime, fundingRate, ...(value.markPrice !== undefined && value.markPrice !== '' ? { markPrice: positive(value.markPrice) } : {}) };
    });
    return rows.sort((a, b) => a.fundingTime - b.fundingTime);
  }
  status() {
    return { running: this.running, paused: this.paused, startedAt: this.startedAt || null, native: Capacitor.isNativePlatform(), queuedRequests: this.queue.length, activeRequests: this.inFlight, cachedCandles: [...this.cache.values()].reduce((total, entry) => total + entry.rows.length, 0) };
  }
}

export const localMarket = new LocalMarketService();
export const startLocalMarket = () => localMarket.start();
export const stopLocalMarket = () => localMarket.stop();
export const pauseLocalMarket = () => localMarket.pause();
export const resumeLocalMarket = () => localMarket.resume();
