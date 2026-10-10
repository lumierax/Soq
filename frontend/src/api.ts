import type { Analysis, BacktestResult, BotConfig, BotMode, BotStatus, Candle, Horizon, Market, MarketSnapshot } from './types';
import { localMarket } from './local-market';
import { runLocalBacktest } from './local-backtest';

export class ApiError extends Error {
  constructor(message: string, public status = 0) {
    super(message);
    this.name = 'ApiError';
  }
}

function endpoint(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/+$/, '')}${path}`;
}

async function request<T>(baseUrl: string, path: string, options: RequestInit = {}, token?: string, timeoutMs = 25000): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  const timeout = window.setTimeout(() => { timedOut = true; abort(); }, timeoutMs);
  try {
    const headers = new Headers(options.headers);
    headers.set('Accept', 'application/json');
    if (options.body) headers.set('Content-Type', 'application/json');
    if (token) {
      const destination = new URL(endpoint(baseUrl, path), window.location.href);
      if (destination.protocol !== 'https:' && !['localhost', '127.0.0.1', '[::1]'].includes(destination.hostname)) {
        throw new ApiError('اتصال البوت يحتاج رابط HTTPS آمن.');
      }
      headers.set('Authorization', `Bearer ${token}`);
    }
    const response = await fetch(endpoint(baseUrl, path), { ...options, headers, signal: controller.signal, cache: 'no-store', credentials: 'omit' });
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data && typeof data === 'object' && 'detail' in data ? (data as { detail: unknown }).detail : null;
      const message = typeof detail === 'string' ? detail : response.status === 401 ? 'رمز الوصول غير صالح.' : response.status === 451 ? 'بيانات Binance غير متاحة من موقع الخادم الحالي.' : `تعذر إكمال الطلب (${response.status}).`;
      throw new ApiError(message, response.status);
    }
    if (data === null) throw new ApiError('الخادم لم يُرجع بيانات صالحة.');
    return data as T;
  } catch (error) {
    if (timedOut && !options.signal?.aborted) throw new ApiError('انتهت مهلة الاتصال بالخادم. أعد المحاولة.');
    if (error instanceof ApiError || (error instanceof DOMException && error.name === 'AbortError')) throw error;
    throw new ApiError('تعذر الاتصال بالخادم. تحقق من الرابط والاتصال.');
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener('abort', abort);
  }
}

const query = (values: Record<string, string>) => new URLSearchParams(values).toString();
export const isDirectMarket = (baseUrl: string) => baseUrl.trim() === '';
export const fetchMarket = (baseUrl: string, market: Market, horizon: Horizon, signal?: AbortSignal) => isDirectMarket(baseUrl) ? localMarket.snapshot(market, horizon, signal) : request<MarketSnapshot>(baseUrl, `/api/markets?${query({ market, horizon })}`, { signal });
export const fetchCandles = (baseUrl: string, market: Market, symbol: string, interval: string, signal?: AbortSignal): Promise<Candle[]> => isDirectMarket(baseUrl) ? localMarket.candles(market, symbol, interval, 500, signal) : request<Candle[]>(baseUrl, `/api/candles?${query({ market, symbol, interval })}`, { signal });
export const fetchAnalysis = (baseUrl: string, market: Market, symbol: string, horizon: Horizon, signal?: AbortSignal) => isDirectMarket(baseUrl) ? localMarket.analyzeSymbol(market, symbol, horizon, signal) : request<Analysis>(baseUrl, `/api/analysis?${query({ market, symbol, horizon })}`, { signal });
const historicalCache = new Map<string, { at: number; result: BacktestResult }>();
export const fetchBacktest = async (baseUrl: string, market: Market, symbol: string, horizon: Horizon, signal?: AbortSignal): Promise<BacktestResult> => {
  if (!isDirectMarket(baseUrl)) return request<BacktestResult>(baseUrl, `/api/backtest?${query({ market, symbol, horizon })}`, { signal }, undefined, 120000);
  const snapshot = await localMarket.snapshot(market, horizon, signal);
  if (!snapshot.items.some(item => item.symbol === symbol)) throw new ApiError('الزوج غير متاح في قائمة Binance الحالية.');
  const key = `${market}:${symbol}:${horizon}`; const cached = historicalCache.get(key);
  if (cached && Date.now() - cached.at < 900_000) return cached.result;
  const result = await runLocalBacktest({ market, symbol, horizon, signal,
    loadCandles: (requestedMarket, requestedSymbol, interval, limit, abortSignal) => localMarket.closedCandles(requestedMarket, requestedSymbol, interval, limit, abortSignal),
    loadFunding: (requestedSymbol, limit, abortSignal) => localMarket.historicalFunding(requestedSymbol, limit, abortSignal),
  });
  if (signal?.aborted) throw new DOMException('تم إلغاء الاختبار.', 'AbortError');
  if (historicalCache.size >= 32) historicalCache.delete(historicalCache.keys().next().value!);
  historicalCache.set(key, { at: Date.now(), result }); return result;
};
function requireBotServer(baseUrl: string) {
  if (isDirectMarket(baseUrl)) throw new ApiError('البوت المستمر يحتاج خادمًا متصلًا. وضع Binance المباشر يعرض السوق والتحليل دون تنفيذ أوامر.');
}
export const fetchBot = (baseUrl: string, token: string, signal?: AbortSignal) => { requireBotServer(baseUrl); return request<BotStatus>(baseUrl, '/api/bot', { signal }, token); };
export const configureBot = (baseUrl: string, token: string, config: BotConfig) => { requireBotServer(baseUrl); return request<BotStatus>(baseUrl, '/api/bot/config', { method: 'POST', body: JSON.stringify(config) }, token); };
export const armBot = (baseUrl: string, token: string, mode: BotMode, confirmation = '') => { requireBotServer(baseUrl); return request<BotStatus>(baseUrl, '/api/bot/arm', { method: 'POST', body: JSON.stringify({ mode, confirmation }) }, token); };
export const pauseBot = (baseUrl: string, token: string) => { requireBotServer(baseUrl); return request<BotStatus>(baseUrl, '/api/bot/pause', { method: 'POST' }, token); };
export const closePosition = (baseUrl: string, token: string, id: string) => { requireBotServer(baseUrl); return request<BotStatus>(baseUrl, `/api/bot/close/${encodeURIComponent(id)}`, { method: 'POST' }, token); };
