/**
 * On-device counterpart of backend/app/analysis.py, strategy version 1.0.
 * Only supplied, confirmed closed candles are analysed. Scores rank technical
 * setups; they are never a probability of success or an invented return.
 * This module does no networking and handles no account credentials.
 */
import type { Analysis, Candle, Horizon, Market, Side } from './types';

export const STRATEGY_VERSION = '1.0';
export const MIN_CANDLES = 100;
export const INTERVAL_MS = {
  '3m': 180_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
  '1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000, '1w': 604_800_000,
} as const;
export type Interval = keyof typeof INTERVAL_MS;
export type Trend = Analysis['trend'];
export type CandleWithClose = Candle & { closeTime?: number };
export type ConfirmedCandle = Candle & { closeTime: number };
export const REQUIRED_TIMEFRAMES = {
  'spot:investment': ['1w', '1d', '4h'],
  'spot:swing': ['1d', '4h'],
  'futures:intraday': ['1h', '15m', '3m'],
} as const satisfies Record<string, readonly Interval[]>;

export class AnalysisUnavailable extends Error {
  constructor(message: string) { super(message); this.name = 'AnalysisUnavailable'; }
}

export interface AnalysisOptions {
  symbol: string;
  market: Market;
  horizon: Horizon;
  frames: Partial<Record<Interval, readonly CandleWithClose[]>>;
  ticker?: { price?: number | null; quoteVolume?: number | null };
  btcRegime?: string;
  fundingRate?: number | null;
  nowMs?: number;
}

interface FrameIndicators {
  trend: Trend;
  rsi: number;
  adx: number;
  atr: number;
  ema21: number;
  ema55: number;
  macd: number;
  macdHistogram: number;
  plusDI: number;
  minusDI: number;
  volumeRatio: number;
  slope: number;
}

function numeric(value: unknown): number {
  if (value === null || value === undefined || (typeof value !== 'number' && typeof value !== 'string' && typeof value !== 'boolean') || (typeof value === 'string' && value.trim() === '')) {
    throw new Error('Invalid number');
  }
  const result = Number(value);
  if (!Number.isFinite(result)) throw new Error('Invalid number');
  return result;
}

/** Python 3.12 float sum uses Neumaier compensation for its float path. */
function sum(values: readonly number[]): number {
  let result = 0;
  let compensation = 0;
  for (const value of values) {
    const next = result + value;
    compensation += Math.abs(result) >= Math.abs(value) ? (result - next) + value : (value - next) + result;
    result = next;
  }
  return compensation && Number.isFinite(compensation) ? result + compensation : result;
}

// Array.prototype.at requires iOS 15.4; the native project supports iOS 15.0.
function at<T>(values: readonly T[], index = -1): T | undefined {
  return values[index < 0 ? values.length + index : index];
}

/**
 * Python-compatible decimal rounding of an IEEE-754 number, including ties
 * to even. Integer rational arithmetic avoids JS multiplication introducing
 * a false tie: Python round(2.675, 2) is 2.67, not Math.round(267.5)/100.
 */
export function pythonRound(value: number, digits = 0): number {
  if (!Number.isFinite(value) || value === 0) return value;
  if (!Number.isInteger(digits) || digits < -308 || digits > 308) throw new RangeError('Unsupported rounding precision');
  const buffer = new ArrayBuffer(8);
  const view = new DataView(buffer);
  view.setFloat64(0, Math.abs(value));
  const bits = view.getBigUint64(0);
  const exponentBits = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & ((1n << 52n) - 1n);
  const mantissa = exponentBits ? (1n << 52n) | fraction : fraction;
  const exponent = exponentBits ? exponentBits - 1023 - 52 : -1074;
  let numerator = mantissa;
  let denominator = 1n;
  if (exponent >= 0) numerator <<= BigInt(exponent);
  else denominator <<= BigInt(-exponent);
  if (digits >= 0) numerator *= 10n ** BigInt(digits);
  else denominator *= 10n ** BigInt(-digits);
  let quotient = numerator / denominator;
  const remainder = numerator % denominator;
  if (2n * remainder > denominator || (2n * remainder === denominator && (quotient & 1n) === 1n)) quotient += 1n;
  let decimal = quotient.toString();
  if (digits > 0) {
    decimal = decimal.padStart(digits + 1, '0');
    decimal = `${decimal.slice(0, -digits)}.${decimal.slice(-digits)}`;
  } else if (digits < 0) decimal += '0'.repeat(-digits);
  return Number(value < 0 ? `-${decimal}` : decimal);
}

export function ema(values: readonly number[], period: number): number[] {
  if (!values.length) return [];
  const alpha = 2 / (period + 1);
  const result = [values[0]];
  for (const value of values.slice(1)) result.push(alpha * value + (1 - alpha) * result[result.length - 1]);
  return result;
}

export function wilder(values: readonly number[], period: number): number[] {
  const result = Array<number>(values.length).fill(NaN);
  if (values.length < period) return result;
  result[period - 1] = sum(values.slice(0, period)) / period;
  for (let index = period; index < values.length; index++) result[index] = (result[index - 1] * (period - 1) + values[index]) / period;
  return result;
}

export function rsi(values: readonly number[], period = 14): number {
  if (values.length <= period) return NaN;
  const changes = values.slice(1).map((right, index) => right - values[index]);
  const gain = at(wilder(changes.map(delta => Math.max(delta, 0)), period))!;
  const loss = at(wilder(changes.map(delta => Math.max(-delta, 0)), period))!;
  if (loss === 0) return gain > 0 ? 100 : 50;
  return 100 - 100 / (1 + gain / loss);
}

export function atrAdx(candles: readonly Candle[], period = 14): [number, number, number, number] {
  const trueRanges: number[] = [], plusMoves: number[] = [], minusMoves: number[] = [];
  for (let index = 1; index < candles.length; index++) {
    const prior = candles[index - 1], candle = candles[index];
    trueRanges.push(Math.max(candle.high - candle.low, Math.abs(candle.high - prior.close), Math.abs(candle.low - prior.close)));
    const up = candle.high - prior.high, down = prior.low - candle.low;
    plusMoves.push(up > down && up > 0 ? up : 0);
    minusMoves.push(down > up && down > 0 ? down : 0);
  }
  const atrValues = wilder(trueRanges, period), plusValues = wilder(plusMoves, period), minusValues = wilder(minusMoves, period);
  const dx: number[] = [];
  let latestPlus = 0, latestMinus = 0;
  for (let index = 0; index < atrValues.length; index++) {
    const trueRange = atrValues[index];
    if (!Number.isFinite(trueRange)) continue;
    latestPlus = trueRange > 0 ? 100 * plusValues[index] / trueRange : 0;
    latestMinus = trueRange > 0 ? 100 * minusValues[index] / trueRange : 0;
    const denominator = latestPlus + latestMinus;
    dx.push(denominator ? 100 * Math.abs(latestPlus - latestMinus) / denominator : 0);
  }
  return [at(atrValues) ?? NaN, at(wilder(dx, period)) ?? NaN, latestPlus, latestMinus];
}

export function closedCandles(candles: readonly CandleWithClose[], interval: Interval, nowMs = Date.now()): ConfirmedCandle[] {
  if (!Object.prototype.hasOwnProperty.call(INTERVAL_MS, interval)) throw new AnalysisUnavailable('إطار زمني غير مدعوم');
  const duration = INTERVAL_MS[interval];
  const result = new Map<number, ConfirmedCandle>();
  for (const original of candles) {
    try {
      const timestamp = Math.trunc(numeric(original.time));
      const closeTime = Math.trunc(numeric(original.closeTime === undefined ? timestamp * 1000 + duration - 1 : original.closeTime));
      if (closeTime >= nowMs) continue;
      const candle = {
        time: timestamp, closeTime, open: numeric(original.open), high: numeric(original.high),
        low: numeric(original.low), close: numeric(original.close), volume: numeric(original.volume),
      };
      if (Math.min(candle.open, candle.high, candle.low, candle.close) <= 0 || candle.volume < 0) throw new Error('Invalid candle value');
      if (candle.high < Math.max(candle.open, candle.close, candle.low) || candle.low > Math.min(candle.open, candle.close, candle.high)) throw new Error('Invalid OHLC ordering');
      if (closeTime !== timestamp * 1000 + duration - 1) throw new Error('Invalid close time');
      result.set(timestamp, candle);
    } catch {
      throw new AnalysisUnavailable('بيانات شموع غير صالحة');
    }
  }
  return [...result.values()].sort((left, right) => left.time - right.time);
}

export function frameIndicators(candles: readonly Candle[]): FrameIndicators {
  const closes = candles.map(candle => candle.close);
  const fast = ema(closes, 21), slow = ema(closes, 55), ema12 = ema(closes, 12), ema26 = ema(closes, 26);
  const macd = ema12.map((left, index) => left - ema26[index]);
  const signal = ema(macd, 9);
  const histogram = at(macd)! - at(signal)!;
  const [atr, adx, plusDI, minusDI] = atrAdx(candles);
  const slope = (at(slow)! - at(slow, -6)!) / at(slow, -6)!;
  let trend: Trend = 'neutral';
  if (at(closes)! > at(fast)! && at(fast)! > at(slow)! && slope > 0) trend = 'bullish';
  else if (at(closes)! < at(fast)! && at(fast)! < at(slow)! && slope < 0) trend = 'bearish';
  const baselineVolume = sum(candles.slice(-21, -1).map(candle => candle.volume)) / 20;
  return {
    trend, rsi: rsi(closes), adx, atr, ema21: at(fast)!, ema55: at(slow)!,
    macd: at(macd)!, macdHistogram: histogram, plusDI, minusDI,
    volumeRatio: baselineVolume > 0 ? at(candles)!.volume / baselineVolume : 0, slope,
  };
}

export function regimeFromCandles(candles: readonly CandleWithClose[], interval: Interval, nowMs = Date.now()): Trend | 'unknown' {
  const values = closedCandles(candles, interval, nowMs);
  if (values.length < MIN_CANDLES || nowMs >= at(values)!.closeTime + INTERVAL_MS[interval] + 1) return 'unknown';
  return frameIndicators(values).trend;
}

export function analyzeCandles(options: AnalysisOptions): Analysis {
  const { symbol, market, horizon, frames, ticker = {}, btcRegime = 'unknown', fundingRate = null, nowMs = Date.now() } = options;
  const key = `${market}:${horizon}`;
  const required = (REQUIRED_TIMEFRAMES as Partial<Record<string, readonly Interval[]>>)[key];
  if (!required) throw new AnalysisUnavailable('قسم أو أفق تداول غير مدعوم');
  const validated: Partial<Record<Interval, ConfirmedCandle[]>> = {};
  const indicators: Partial<Record<Interval, FrameIndicators>> = {};
  for (const interval of required) {
    if (!(interval in frames)) throw new AnalysisUnavailable(`بيانات ${interval} غير متاحة`);
    const values = closedCandles(frames[interval]!, interval, nowMs);
    if (values.length < MIN_CANDLES) throw new AnalysisUnavailable(`يلزم ${MIN_CANDLES} شمعة مغلقة على ${interval}`);
    if (nowMs >= at(values)!.closeTime + INTERVAL_MS[interval] + 1) throw new AnalysisUnavailable(`بيانات ${interval} قديمة`);
    const recent = values.slice(-MIN_CANDLES);
    if (recent.slice(1).some((right, index) => right.time - recent[index].time !== Math.floor(INTERVAL_MS[interval] / 1000))) throw new AnalysisUnavailable(`شموع ${interval} غير متصلة`);
    validated[interval] = values;
    indicators[interval] = frameIndicators(values);
  }
  const executionInterval = at(required)!;
  const execution = indicators[executionInterval]!;
  const candles = validated[executionInterval]!;
  const close = at(candles)!.close;
  let entry: number, quoteVolume: number;
  try {
    entry = numeric(ticker.price === undefined ? close : ticker.price);
    quoteVolume = numeric(ticker.quoteVolume === undefined ? 0 : ticker.quoteVolume);
    if (entry <= 0 || quoteVolume < 0) throw new Error('Invalid ticker');
  } catch { throw new AnalysisUnavailable('بيانات السعر والسيولة غير صالحة'); }
  const atr = execution.atr;
  if (atr <= 0 || !Number.isFinite(atr)) throw new AnalysisUnavailable('التذبذب غير قابل للتحليل');
  const bullish = Object.values(indicators).filter(item => item.trend === 'bullish').length;
  const bearish = Object.values(indicators).filter(item => item.trend === 'bearish').length;
  let direction: Side = bullish > bearish ? 'long' : bearish > bullish ? 'short' : 'wait';
  if (market === 'spot' && direction === 'short') direction = 'wait';
  const trend: Trend = bullish === required.length ? 'bullish' : bearish === required.length ? 'bearish' : 'neutral';
  const reasons: string[] = [], warnings: string[] = [], blockers: string[] = [];
  const aligned = direction === 'long' ? bullish : direction === 'short' ? bearish : 0;
  let score = 32 * aligned / required.length;
  if (aligned === required.length) reasons.push('الاتجاه متوافق عبر جميع الأطر');
  else blockers.push('الأطر الزمنية لم تتوافق بعد');
  score += 14 * Math.max(0, Math.min(1, (execution.adx - 12) / 18));
  if (execution.adx >= 22) reasons.push('قوة اتجاه واضحة بحسب ADX');
  else if (execution.adx < 16) blockers.push('اتجاه ضعيف أو سوق عرضي');
  const momentum = execution.rsi;
  const rsiOK = direction === 'long' ? 48 <= momentum && momentum <= 74 : 26 <= momentum && momentum <= 52;
  if (rsiOK) { score += 12; reasons.push('الزخم يدعم الاتجاه دون تشبع شديد'); }
  else if (momentum >= 80 || momentum <= 20) blockers.push('تشبع شديد؛ انتظار تهدئة الزخم');
  else warnings.push('الزخم يحتاج تأكيداً إضافياً');
  const macdOK = direction === 'long' ? execution.macd > 0 && execution.macdHistogram >= 0 : execution.macd < 0 && execution.macdHistogram <= 0;
  if (direction !== 'wait' && macdOK) { score += 12; reasons.push('MACD يؤكد الزخم'); }
  else warnings.push('MACD لا يؤكد استمرار الحركة');
  const volumeRatio = execution.volumeRatio;
  score += 10 * Math.min(1, Math.max(0, volumeRatio - 0.5) / 0.8);
  if (volumeRatio >= 1.2) reasons.push('الحركة مدعومة بحجم تداول مرتفع');
  else if (volumeRatio < 0.65) blockers.push('حجم التداول ضعيف');
  else warnings.push('حجم التداول قريب من المعتاد');
  const prior = candles.slice(-21, -1);
  const upper = Math.max(...prior.map(candle => candle.high)), lower = Math.min(...prior.map(candle => candle.low));
  const breakout = direction === 'long' ? close > upper : close < lower;
  const pullback = Math.abs(close - execution.ema21) <= 1.2 * atr;
  if (direction !== 'wait' && (breakout || pullback)) {
    score += 10;
    reasons.push(breakout ? 'اختراق بنية سعرية' : 'السعر قريب من منطقة إعادة اختبار الاتجاه');
  } else warnings.push('السعر بعيد عن منطقة دخول واضحة');
  const matchingRegime = direction === 'long' ? 'bullish' : 'bearish';
  if (btcRegime === matchingRegime) { score += 5; reasons.push('اتجاه بيتكوين يدعم الإعداد'); }
  else if (btcRegime === 'unknown') blockers.push('سياق بيتكوين غير متاح أو قديم');
  else if (btcRegime !== 'neutral' && btcRegime !== matchingRegime && symbol !== 'BTCUSDT') {
    warnings.push('اتجاه بيتكوين معاكس؛ احتمال انعكاس أعلى'); score -= 8;
  }
  const minVolume = market === 'futures' ? 20_000_000 : 5_000_000;
  if (quoteVolume >= minVolume) { score += 5; reasons.push('سيولة يومية كافية مبدئياً'); }
  else blockers.push('سيولة USDT اليومية أقل من حد الاستراتيجية');
  if (market === 'futures') {
    if (fundingRate === null || !Number.isFinite(fundingRate)) blockers.push('بيانات التمويل غير متاحة');
    else if ((direction === 'long' && fundingRate > 0.0008) || (direction === 'short' && fundingRate < -0.0008)) {
      warnings.push('تمويل مرتفع ضد جهة الصفقة'); score -= 10;
    }
  }
  if (Math.abs(entry - close) > atr) blockers.push('تحرك السعر بعيداً عن آخر شمعة؛ يلزم تأكيد جديد');
  const atrPercent = atr / entry * 100;
  if (atrPercent > (market === 'futures' ? 5 : 14)) blockers.push('تذبذب استثنائي؛ تكلفة الوقف غير مستقرة');
  const short = direction === 'short';
  const pivot = short ? Math.max(...candles.slice(-12).map(candle => candle.high)) : Math.min(...candles.slice(-12).map(candle => candle.low));
  const stop = short ? Math.max(pivot + atr * 0.25, entry + atr * 1.4) : Math.min(pivot - atr * 0.25, entry - atr * 1.4);
  const risk = Math.abs(entry - stop);
  if (stop <= 0) throw new AnalysisUnavailable('لا يمكن بناء وقف صالح من البنية والتذبذب الحاليين');
  if (risk > atr * 5) blockers.push('وقف البنية بعيد؛ انتظار دخول أفضل');
  const sign = short ? -1 : 1;
  let target1 = entry + sign * risk * 2.2;
  const barriers = candles.slice(-80, -1).map(candle => short ? candle.low : candle.high);
  const viable = barriers.filter(value => short ? target1 < value && value < entry - risk * 0.5 : entry + risk * 0.5 < value && value < target1);
  if (viable.length) {
    target1 = short ? Math.max(...viable) : Math.min(...viable);
    warnings.push('منطقة عرض أو طلب تاريخية تحد الهدف الأول');
  }
  const riskReward = risk ? Math.abs(target1 - entry) / risk : 0;
  if (riskReward < 1.8) blockers.push('العائد إلى المخاطرة غير كافٍ قبل منطقة العرض أو الطلب');
  const targets = [target1, entry + sign * risk * 3.3, entry + sign * risk * 4.5];
  if (targets.some(value => value <= 0)) throw new AnalysisUnavailable('لا يمكن بناء أهداف صالحة من البنية والتذبذب الحاليين');
  score = pythonRound(Math.max(0, Math.min(100, score)));
  if (score < 70) blockers.push('قوة الإعداد أقل من حد التفعيل');
  const side = direction !== 'wait' && !blockers.length ? direction : 'wait';
  warnings.push(...blockers);
  const closeTime = at(candles)!.closeTime;
  return {
    symbol, market, horizon, asOf: nowMs,
    validUntil: Math.min(nowMs + INTERVAL_MS[executionInterval], closeTime + INTERVAL_MS[executionInterval] + 1),
    side, score, entry, stop, targets, riskReward: pythonRound(riskReward, 2),
    atrPercent: pythonRound(atrPercent, 3), trend, reasons, warnings: [...new Set(warnings)],
    timeframes: required.map(interval => ({
      interval, trend: indicators[interval]!.trend,
      rsi: pythonRound(indicators[interval]!.rsi, 1), adx: pythonRound(indicators[interval]!.adx, 1),
    })),
    strategyVersion: STRATEGY_VERSION, closedCandleTime: closeTime,
  };
}
