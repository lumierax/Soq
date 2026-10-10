/** Closed-candle, point-in-time replay on the device. No credentials or invented returns. */
import type { Analysis, BacktestResult, Horizon, Market } from './types';
import {
  AnalysisUnavailable,
  INTERVAL_MS,
  MIN_CANDLES,
  REQUIRED_TIMEFRAMES,
  analyzeCandles,
  closedCandles,
  pythonRound as round,
  regimeFromCandles,
  type CandleWithClose,
  type ConfirmedCandle,
  type Interval,
} from './local-analysis';

export interface HistoricalFunding {
  fundingTime: number;
  fundingRate: number;
  markPrice?: number;
}

export interface BacktestTrade {
  side: 'long' | 'short';
  entry: number;
  exit: number;
  stop: number;
  target: number;
  openedAt: number;
  closedAt: number;
  pnl: number;
  reason: 'stop' | 'target' | 'end_of_sample';
  fundingPnl: number;
}

export interface LocalBacktestResult extends BacktestResult {
  symbol: string;
  market: Market;
  horizon: Horizon;
  strategyVersion: string;
  trades: BacktestTrade[];
  slippageBps: number;
  evaluatedCandles: number;
  skippedCandles: number;
  eligibleCandles: number;
}

export interface LocalBacktestOptions {
  market: Market;
  symbol: string;
  horizon: Horizon;
  loadCandles: (
    market: Market, symbol: string, interval: Interval, limit: number, signal?: AbortSignal,
  ) => Promise<readonly CandleWithClose[]>;
  loadFunding?: (symbol: string, limit: number, signal?: AbortSignal) => Promise<readonly HistoricalFunding[]>;
  signal?: AbortSignal;
}

export interface ReplayOptions {
  market: Market;
  symbol: string;
  horizon: Horizon;
  frames: Partial<Record<Interval, readonly CandleWithClose[]>>;
  btcFrames: readonly CandleWithClose[];
  funding?: readonly HistoricalFunding[];
  nowMs?: number;
  signal?: AbortSignal;
}

/** An injection seam for deterministic chronology and execution tests; the app uses the real engine. */
export interface ReplayEngine {
  analyze: typeof analyzeCandles;
  regime: typeof regimeFromCandles;
}

interface Position {
  entry: number;
  stop: number;
  target: number;
  quantity: number;
  sign: 1 | -1;
  entryFee: number;
  openedAt: number;
  fundingPnl: number;
}

const DEFAULT_ENGINE: ReplayEngine = { analyze: analyzeCandles, regime: regimeFromCandles };
const REQUEST_LIMIT = 1000;
const MAX_REPLAY_BARS = 1000;

function requiredFrames(market: Market, horizon: Horizon): readonly Interval[] {
  const required = REQUIRED_TIMEFRAMES[`${market}:${horizon}` as keyof typeof REQUIRED_TIMEFRAMES];
  if (!required) throw new AnalysisUnavailable('أفق الاختبار غير مدعوم');
  return required;
}

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('أُلغي الاختبار التاريخي', 'AbortError');
}

function lowerBound(values: readonly number[], target: number) {
  let low = 0;
  let high = values.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (values[middle] < target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function upperBound(values: readonly number[], target: number) {
  let low = 0;
  let high = values.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (values[middle] <= target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function validatedFunding(records: readonly HistoricalFunding[], nowMs: number) {
  const unique = new Map<number, HistoricalFunding>();
  for (const record of records) {
    if (!Number.isSafeInteger(record.fundingTime) || record.fundingTime <= 0 || !Number.isFinite(record.fundingRate) ||
      (record.markPrice !== undefined && (!Number.isFinite(record.markPrice) || record.markPrice <= 0))) {
      throw new AnalysisUnavailable('بيانات التمويل التاريخية غير صالحة');
    }
    if (record.fundingTime <= nowMs) unique.set(record.fundingTime, { ...record });
  }
  return [...unique.values()].sort((left, right) => left.fundingTime - right.fundingTime);
}

/**
 * Same execution rules as the server replay: decide at close, enter next open,
 * adverse fills, stop before target on ambiguous bars, 1% risk and 1× notional.
 * The short bounded yields keep input and chart gestures responsive on iPhone.
 */
export async function simulateLocalBacktest(
  options: ReplayOptions,
  engine: ReplayEngine = DEFAULT_ENGINE,
): Promise<LocalBacktestResult> {
  const { market, symbol, horizon, signal } = options;
  throwIfAborted(signal);
  const nowMs = options.nowMs ?? Date.now();
  if (!Number.isSafeInteger(nowMs) || nowMs <= 0) throw new AnalysisUnavailable('وقت الاختبار غير صالح');
  const required = requiredFrames(market, horizon);
  const cleaned: Partial<Record<Interval, ConfirmedCandle[]>> = {};
  for (const interval of required) {
    cleaned[interval] = closedCandles(options.frames[interval] ?? [], interval, nowMs).slice(-MAX_REPLAY_BARS);
  }
  const baseInterval = required[required.length - 1];
  const regimeInterval = required[0];
  const base = cleaned[baseInterval]!;
  const btc = closedCandles(options.btcFrames, regimeInterval, nowMs).slice(-MAX_REPLAY_BARS);
  if (required.some(interval => cleaned[interval]!.length < MIN_CANDLES + 2) || btc.length < MIN_CANDLES) {
    throw new AnalysisUnavailable('العينة التاريخية غير كافية للاختبار متعدد الأطر');
  }
  // Missing execution bars hide stop/target touches and funding, and could
  // delay an entry until its setup expires. Never invent an intragap path.
  if (base.some((candle, index) => index > 0 && candle.closeTime - base[index - 1].closeTime !== INTERVAL_MS[baseInterval])) {
    throw new AnalysisUnavailable('سجل إطار التنفيذ يحتوي على فجوات؛ لا يمكن إصدار نتيجة تاريخية موثوقة');
  }
  const times: Partial<Record<Interval, number[]>> = {};
  for (const interval of required) times[interval] = cleaned[interval]!.map(candle => candle.closeTime);
  const btcTimes = btc.map(candle => candle.closeTime);
  const funding = validatedFunding(options.funding ?? [], nowMs);
  const fundingTimes = funding.map(record => record.fundingTime);
  const feesBps = market === 'spot' ? 10 : 5;
  const fee = feesBps / 10_000;
  const slippage = 2 / 10_000;
  const initial = 10_000;
  let equity = initial;
  let peak = initial;
  let maxDrawdown = 0;
  const trades: BacktestTrade[] = [];
  let position: Position | null = null;
  let pending: Analysis | null = null;
  let eligible = 0;
  let evaluated = 0;
  let skipped = 0;
  let missingFunding = 0;
  let sampleStart: number | null = null;

  const closeTrade = (rawExit: number, candle: ConfirmedCandle, reason: BacktestTrade['reason']) => {
    if (!position) throw new Error('لا يوجد مركز مفتوح');
    const execution = rawExit * (1 - position.sign * slippage);
    const gross = position.sign * (execution - position.entry) * position.quantity;
    const exitFee = execution * position.quantity * fee;
    equity += gross - exitFee;
    const net = gross - exitFee - position.entryFee + position.fundingPnl;
    trades.push({
      side: position.sign === 1 ? 'long' : 'short',
      entry: position.entry,
      exit: execution,
      stop: position.stop,
      target: position.target,
      openedAt: position.openedAt,
      closedAt: candle.closeTime,
      pnl: round(net, 4),
      reason,
      fundingPnl: round(position.fundingPnl, 4),
    });
    position = null;
  };

  for (let index = MIN_CANDLES; index < base.length; index++) {
    throwIfAborted(signal);
    if ((index - MIN_CANDLES) % 16 === 0) {
      await new Promise<void>(resolve => setTimeout(resolve, 0));
      throwIfAborted(signal);
    }
    const candle = base[index];
    const openTime = Math.trunc(candle.time * 1000);
    const decisionTime = candle.closeTime + 1;
    if (pending && position === null) {
      const sign = pending.side === 'long' ? 1 : -1;
      const entry = candle.open * (1 + sign * slippage);
      const stop = pending.stop;
      const target = pending.targets[0];
      const distance = sign * (entry - stop);
      const reward = sign * (target - entry);
      // Reject a gap that destroys the setup; never move its stop to improve results.
      if (distance > 0 && reward / distance >= 1.8 && equity > 0 && Number.isFinite(target)) {
        const quantity = Math.min(equity * 0.01 / distance, equity / entry);
        const entryFee = quantity * entry * fee;
        equity -= entryFee;
        position = { entry, stop, target, quantity, sign, entryFee, openedAt: openTime, fundingPnl: 0 };
      }
      pending = null;
    }

    if (position) {
      const sign = position.sign;
      const stopHit = sign === 1 ? candle.low <= position.stop : candle.high >= position.stop;
      const targetHit = sign === 1 ? candle.high >= position.target : candle.low <= position.target;
      const gapStop = sign === 1 ? candle.open <= position.stop : candle.open >= position.stop;
      const fundingStart = lowerBound(fundingTimes, Math.max(openTime, position.openedAt));
      const fundingEnd = upperBound(fundingTimes, candle.closeTime);
      for (const record of funding.slice(fundingStart, fundingEnd)) {
        const mark = record.markPrice ?? candle.open;
        let payment = -sign * record.fundingRate * mark * position.quantity;
        // The intrabar sequence is unknown: charge adverse funding, do not credit favorable funding.
        if ((stopHit || targetHit) && payment > 0) payment = 0;
        equity += payment;
        position.fundingPnl += payment;
      }
      if (stopHit) closeTrade(gapStop ? candle.open : position.stop, candle, 'stop');
      else if (targetHit) closeTrade(position.target, candle, 'target');
    }

    let markEquity = equity;
    if (position) {
      markEquity += position.sign * (candle.close - position.entry) * position.quantity;
      markEquity -= candle.close * position.quantity * fee;
    }
    peak = Math.max(peak, markEquity);
    maxDrawdown = Math.max(maxDrawdown, (peak - markEquity) / peak * 100);
    if (equity <= 0 || position || index === base.length - 1) continue;

    const aligned: Partial<Record<Interval, readonly ConfirmedCandle[]>> = {};
    for (const interval of required) {
      const end = lowerBound(times[interval]!, decisionTime);
      aligned[interval] = cleaned[interval]!.slice(Math.max(0, end - 280), end);
    }
    const btcEnd = lowerBound(btcTimes, decisionTime);
    const historicalBtc = btc.slice(Math.max(0, btcEnd - 280), btcEnd);
    if (required.some(interval => aligned[interval]!.length < MIN_CANDLES) || historicalBtc.length < MIN_CANDLES) {
      skipped++;
      continue;
    }
    sampleStart ??= decisionTime;
    eligible++;
    const dailyCount = Math.max(1, Math.floor(86_400_000 / INTERVAL_MS[baseInterval]));
    const daily = base.slice(Math.max(0, index + 1 - dailyCount), index + 1);
    const quoteVolume = daily.reduce((sum, value) => sum + value.volume * value.close, 0);
    const fundingIndex = upperBound(fundingTimes, decisionTime) - 1;
    const fundingRate = fundingIndex >= 0 && decisionTime - fundingTimes[fundingIndex] <= 86_400_000
      ? funding[fundingIndex].fundingRate : null;
    if (market === 'futures' && fundingRate === null) missingFunding++;
    try {
      const setup = engine.analyze({
        symbol, market, horizon, frames: aligned,
        ticker: { price: candle.close, quoteVolume },
        btcRegime: engine.regime(historicalBtc, regimeInterval, decisionTime),
        fundingRate, nowMs: decisionTime,
      });
      evaluated++;
      if ((setup.side === 'long' || setup.side === 'short') && setup.score >= 75 && setup.riskReward >= 2) pending = setup;
    } catch (error) {
      if (!(error instanceof AnalysisUnavailable)) throw error;
      skipped++;
    }
  }

  if (position) {
    closeTrade(base[base.length - 1].close, base[base.length - 1], 'end_of_sample');
    peak = Math.max(peak, equity);
    maxDrawdown = Math.max(maxDrawdown, (peak - equity) / peak * 100);
  }
  const wins = trades.filter(trade => trade.pnl > 0).map(trade => trade.pnl);
  const losses = trades.filter(trade => trade.pnl < 0).map(trade => -trade.pnl);
  const limitations = [
    'اختبار تاريخي استكشافي لعملة واحدة؛ ليس احتمال نجاح أو ضماناً للأداء القادم.',
    'دخول عند افتتاح الشمعة التالية؛ وقف أولاً إذا لمس السعر الوقف والهدف في الشمعة نفسها.',
    'مخاطرة 1%، دون رافعة، حد قيمة الصفقة كامل رأس المال، واحتساب العمولة وانزلاق 2 نقطة أساس لكل جهة.',
    'السيولة تقديرية من OHLCV التاريخي؛ لا يحاكي دفتر الأوامر أو تأخر الشبكة أو تغير قوائم Binance.',
    'لا يشمل الأخبار أو السحب والإيداع أو إدارة محفظة متعددة العملات أو اختباراً مستقلاً خارج العينة.',
    'يُحسب على الجهاز في المقدمة باستخدام آخر 1000 شمعة متاحة لكل إطار؛ تتوقف المحاكاة عند إلغاء الطلب.',
  ];
  if (market === 'futures') {
    limitations.push('التمويل مأخوذ من التاريخ المتاح؛ لا تتولد إشارة عند غياب سياق التمويل السابق.');
    if (missingFunding) limitations.push(`لم يتوفر سياق تمويل سابق لـ ${missingFunding} شمعة مؤهلة؛ استُبعدت من فرص التنفيذ.`);
    if (!funding.length) limitations.push('لم يتوفر سجل تمويل؛ لا يمكن استنتاج أداء فيوتشر من هذه العينة.');
  }
  if (trades.length < 30) limitations.push('عدد الصفقات أقل من 30؛ العينة لا تكفي لاستنتاج موثوق عن أداء الاستراتيجية.');
  if (horizon === 'investment') limitations.push('مدة شموع 4 ساعات المتاحة قصيرة لتقييم الاستثمار طويل الأجل.');
  if (eligible === 0) limitations.push('لا توجد شموع ذات سياق تاريخي كافٍ ومتزامن؛ الصفر لا يعني أداءً مثبتاً.');
  throwIfAborted(signal);
  return {
    symbol, market, horizon, strategyVersion: '1.0', trades, tradeCount: trades.length,
    winRate: trades.length ? round(wins.length / trades.length * 100, 2) : null,
    netReturn: round((equity / initial - 1) * 100, 3),
    maxDrawdown: round(maxDrawdown, 3),
    profitFactor: losses.length ? round(wins.reduce((sum, value) => sum + value, 0) / losses.reduce((sum, value) => sum + value, 0), 3) : null,
    sampleStart: sampleStart ?? Math.trunc(base[0].time * 1000),
    sampleEnd: base[base.length - 1].closeTime,
    feesBps, slippageBps: 2,
    evaluatedCandles: evaluated, skippedCandles: skipped, eligibleCandles: eligible,
    limitations,
  };
}

/** Load only public Binance history through the caller's bounded native/browser client. */
export async function runLocalBacktest(options: LocalBacktestOptions): Promise<LocalBacktestResult> {
  const { market, symbol, horizon, loadCandles, loadFunding, signal } = options;
  throwIfAborted(signal);
  const required = requiredFrames(market, horizon);
  const nowMs = Date.now();
  if (market === 'futures' && !loadFunding) throw new AnalysisUnavailable('يلزم سجل التمويل التاريخي لاختبار الفيوتشر');
  const [values, btc, funding] = await Promise.all([
    Promise.all(required.map(interval => loadCandles(market, symbol, interval, REQUEST_LIMIT, signal))),
    symbol === 'BTCUSDT' ? Promise.resolve(null) : loadCandles(market, 'BTCUSDT', required[0], REQUEST_LIMIT, signal),
    market === 'futures' ? loadFunding!(symbol, REQUEST_LIMIT, signal) : Promise.resolve([]),
  ]);
  throwIfAborted(signal);
  const frames: Partial<Record<Interval, readonly CandleWithClose[]>> = {};
  required.forEach((interval, index) => { frames[interval] = values[index]; });
  return simulateLocalBacktest({ market, symbol, horizon, frames, btcFrames: btc ?? values[0], funding, nowMs, signal });
}
