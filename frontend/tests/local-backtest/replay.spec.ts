import { test, expect } from '@playwright/test';
import {
  runLocalBacktest,
  simulateLocalBacktest,
  type ReplayEngine,
} from '../../src/local-backtest';
import { AnalysisUnavailable, INTERVAL_MS, type CandleWithClose, type Interval } from '../../src/local-analysis';
import type { Analysis, Side } from '../../src/types';

const END = 1_800_000_000_000;

function history(interval: Interval, count: number, end = END): CandleWithClose[] {
  const duration = INTERVAL_MS[interval];
  return Array.from({ length: count }, (_, index) => ({
    time: (end - (count - index) * duration) / 1000,
    closeTime: end - (count - index - 1) * duration - 1,
    open: 100, high: 103, low: 97, close: 100, volume: 1_000_000,
  }));
}

function setup(side: Side, nowMs: number): Analysis {
  return {
    symbol: 'BTCUSDT', market: 'spot', horizon: 'swing', asOf: nowMs, validUntil: nowMs + 14_400_000,
    side, score: side === 'wait' ? 0 : 90, entry: 100, stop: side === 'short' ? 104 : 96,
    targets: side === 'short' ? [91, 88, 82] : [109, 112, 118], riskReward: 2.25, atrPercent: 4,
    trend: 'bullish', reasons: [], warnings: [], timeframes: [], strategyVersion: 'test-only', closedCandleTime: nowMs - 1,
  };
}

test('same-bar target and stop select the loss; entries use the next open and both fees', async () => {
  const daily = history('1d', 122);
  const four = history('4h', 732).map(candle => ({ ...candle, high: 110, low: 95 }));
  const decisions: number[] = [];
  const engine: ReplayEngine = {
    regime: (candles, _interval, nowMs) => {
      expect(candles.every(candle => candle.closeTime! < nowMs!)).toBe(true);
      return 'bullish';
    },
    analyze: ({ frames, nowMs = 0 }) => {
      for (const values of Object.values(frames)) expect(values!.every(candle => candle.closeTime! < nowMs)).toBe(true);
      decisions.push(nowMs);
      return setup('long', nowMs);
    },
  };
  const result = await simulateLocalBacktest({ market: 'spot', symbol: 'BTCUSDT', horizon: 'swing', frames: { '1d': daily, '4h': four }, btcFrames: daily, nowMs: END }, engine);
  expect(result.tradeCount).toBeGreaterThan(0);
  expect(result.winRate).toBe(0);
  expect(result.trades.every(trade => trade.reason === 'stop' && trade.pnl < 0)).toBe(true);
  const first = result.trades[0];
  expect(first.openedAt).toBe(decisions[0]);
  expect(first.entry).toBeCloseTo(100.02, 8);
  expect(first.exit).toBeCloseTo(95.9808, 8);
  // 1% risk alone is $100. Adverse fills and two spot fees make the loss larger.
  expect(first.pnl).toBeCloseTo(-105.3533, 3);
  expect(result.netReturn).toBeLessThan(0);
  expect(result.maxDrawdown).toBeGreaterThan(0);
});

test('forming and future bars cannot change the replay at a fixed cutoff', async () => {
  const daily = history('1d', 122);
  const four = history('4h', 732);
  const cutoff = four[four.length - 7].closeTime! + 1;
  const engine: ReplayEngine = { analyze: ({ nowMs = 0 }) => setup('wait', nowMs), regime: () => 'neutral' };
  const complete = await simulateLocalBacktest({ market: 'spot', symbol: 'BTCUSDT', horizon: 'swing', frames: { '1d': daily, '4h': four }, btcFrames: daily, nowMs: cutoff }, engine);
  const trimmedDaily = daily.filter(candle => candle.closeTime! < cutoff);
  const trimmedFour = four.filter(candle => candle.closeTime! < cutoff);
  const trimmed = await simulateLocalBacktest({ market: 'spot', symbol: 'BTCUSDT', horizon: 'swing', frames: { '1d': trimmedDaily, '4h': trimmedFour }, btcFrames: trimmedDaily, nowMs: cutoff }, engine);
  expect(complete).toEqual(trimmed);
  expect(complete.tradeCount).toBe(0);
  expect(complete.winRate).toBeNull();
});

test('a short stop gap fills at the adverse open and never credits ambiguous favorable funding', async () => {
  const hourly = history('1h', 250);
  const fifteen = history('15m', 1000);
  const three = history('3m', 1000);
  three[102] = { ...three[102], open: 110, high: 112, low: 107, close: 109 };
  const fundingTime = three[102].time * 1000;
  const replay = async (fundingRate: number) => {
    let decisions = 0;
    const engine: ReplayEngine = { regime: () => 'bearish', analyze: ({ nowMs = 0 }) => setup(decisions++ === 0 ? 'short' : 'wait', nowMs) };
    return simulateLocalBacktest({ market: 'futures', symbol: 'BTCUSDT', horizon: 'intraday',
      frames: { '1h': hourly, '15m': fifteen, '3m': three }, btcFrames: hourly,
      funding: [{ fundingTime: three[100].time * 1000, fundingRate: 0 }, { fundingTime, fundingRate, markPrice: 110 }], nowMs: END,
    }, engine);
  };
  const favorable = await replay(.001);
  const adverse = await replay(-.001);
  expect(favorable.tradeCount).toBe(1);
  expect(favorable.trades[0].reason).toBe('stop');
  expect(favorable.trades[0].entry).toBeCloseTo(99.98, 8);
  expect(favorable.trades[0].exit).toBeCloseTo(110.022, 8);
  expect(favorable.trades[0].openedAt).toBe(three[101].time * 1000);
  expect(favorable.trades[0].fundingPnl).toBe(0);
  expect(adverse.trades[0].fundingPnl).toBeLessThan(0);
  expect(adverse.trades[0].pnl).toBeLessThan(favorable.trades[0].pnl);
});

test('insufficient multi-frame history and missing futures funding loader fail honestly', async () => {
  await expect(simulateLocalBacktest({ market: 'spot', symbol: 'BTCUSDT', horizon: 'swing',
    frames: { '1d': history('1d', 3), '4h': history('4h', 5) }, btcFrames: history('1d', 3), nowMs: END,
  })).rejects.toBeInstanceOf(AnalysisUnavailable);
  let requested = false;
  await expect(runLocalBacktest({ market: 'futures', symbol: 'BTCUSDT', horizon: 'intraday', loadCandles: async () => { requested = true; return []; } })).rejects.toThrow('يلزم سجل التمويل التاريخي');
  expect(requested).toBe(false);
});

test('bounded replay yields to input cancellation instead of returning an unfinished result', async () => {
  const abort = new AbortController();
  const daily = history('1d', 150);
  const four = history('4h', 900);
  const engine: ReplayEngine = { regime: () => 'neutral', analyze: ({ nowMs = 0 }) => setup('wait', nowMs) };
  const simulation = simulateLocalBacktest({ market: 'spot', symbol: 'BTCUSDT', horizon: 'swing', frames: { '1d': daily, '4h': four }, btcFrames: daily, nowMs: END, signal: abort.signal }, engine);
  setTimeout(() => abort.abort(), 0);
  await expect(simulation).rejects.toMatchObject({ name: 'AbortError' });
});

for (const [label, missingIndex] of [['pending entry', 600], ['open position', 601]] as const) {
  test(`missing execution candle during ${label} rejects the entire sample`, async () => {
    const daily = history('1d', 122);
    const four = history('4h', 732);
    let decisions = 0;
    const engine: ReplayEngine = {
      regime: () => 'bullish',
      analyze: ({ nowMs = 0 }) => { decisions++; return setup('long', nowMs); },
    };
    const options = { market: 'spot', symbol: 'BTCUSDT', horizon: 'swing', frames: { '1d': daily, '4h': four }, btcFrames: daily, nowMs: END } as const;
    const baseline = await simulateLocalBacktest(options, engine);
    expect(baseline.tradeCount).toBe(1);
    expect(baseline.trades[0].openedAt).toBe(four[600].time * 1000);
    expect(baseline.trades[0].reason).toBe('end_of_sample');
    decisions = 0;
    const incomplete = [...four.slice(0, missingIndex), ...four.slice(missingIndex + 1)];
    await expect(simulateLocalBacktest({ ...options, frames: { '1d': daily, '4h': incomplete } }, engine)).rejects.toThrow('سجل إطار التنفيذ يحتوي على فجوات');
    expect(decisions).toBe(0);
  });
}
