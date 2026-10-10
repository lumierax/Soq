export type Market = 'spot' | 'futures';
export type Horizon = 'swing' | 'investment' | 'intraday';
export type Side = 'long' | 'short' | 'wait';
export type BotMode = 'paper' | 'testnet' | 'live';

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Analysis {
  symbol: string;
  market: Market;
  horizon: Horizon;
  asOf: number;
  validUntil: number;
  side: Side;
  score: number;
  entry: number;
  stop: number;
  targets: number[];
  riskReward: number;
  atrPercent: number;
  trend: 'bullish' | 'bearish' | 'neutral';
  reasons: string[];
  warnings: string[];
  timeframes: { interval: string; trend: string; rsi: number; adx: number }[];
  strategyVersion: string;
  closedCandleTime: number;
}

export interface MarketItem {
  symbol: string;
  baseAsset: string;
  quoteAsset: string;
  pricePrecision: number;
  quantityPrecision: number;
  tickSize: number;
  stepSize: number;
  minQty: number;
  minNotional: number;
  price: number | null;
  change24h: number | null;
  quoteVolume: number | null;
  high24h: number | null;
  low24h: number | null;
  fundingRate?: number | null;
  analysis: Analysis | null;
}

export interface MarketSnapshot {
  market: Market;
  horizon: Horizon;
  updatedAt: number | null;
  source: 'binance';
  connection: 'live' | 'degraded' | 'offline';
  totalSymbols: number;
  analyzedSymbols: number;
  scanProgress: number;
  btcRegime: string | Record<string, unknown> | null;
  items: MarketItem[];
  error: string | null;
}

export interface BotConfig {
  riskPercent: number;
  maxPositions: number;
  maxDailyLossPercent: number;
  leverage: number;
  minScore: number;
  minRiskReward: number;
  capital: number;
}

export interface BotPosition {
  id: string;
  symbol: string;
  side: string;
  entry: number;
  markPrice: number;
  quantity: number;
  stop: number;
  target: number;
  pnl: number;
  openedAt: number;
  mode: BotMode;
}

export interface BotEvent {
  id: string;
  time: number;
  type: string;
  message: string;
}

export interface BotStatus {
  state: 'stopped' | 'running' | 'paused' | 'error';
  mode: BotMode;
  credentialsConfigured: boolean;
  liveAllowed: boolean;
  config: BotConfig;
  equity: number | null;
  dailyPnl: number | null;
  dailyBaselineComplete?: boolean;
  dailyBaselineAt?: number | null;
  positions: BotPosition[];
  events: BotEvent[];
  error: string | null;
  telegram?: { configured: boolean; running: boolean; queued: number; delivered: number; failed: number; dropped: number; error: string | null };
}

export interface BacktestResult {
  trades: unknown[];
  tradeCount: number;
  winRate: number | null;
  netReturn: number;
  maxDrawdown: number;
  profitFactor: number | null;
  sampleStart: number;
  sampleEnd: number;
  feesBps: number;
  limitations: string[];
}
