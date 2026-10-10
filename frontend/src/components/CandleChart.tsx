import { useEffect, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';
import { fetchCandles } from '../api';
import type { Analysis, Candle } from '../types';

type Market = 'spot' | 'futures';
type FeedState = 'loading' | 'live' | 'polling' | 'offline' | 'preview';

interface CandleChartProps {
  baseUrl: string;
  market: Market;
  symbol: string;
  interval: string;
  analysis?: Analysis | null;
  preview: boolean;
  tickSize?: number;
  pricePrecision?: number;
  onPrice?: (price: number) => void;
}

const INTERVAL_SECONDS: Record<string, number> = {
  '3m': 180,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '4h': 14400,
  '1d': 86400,
  '1w': 604800,
};

const UP = '#27d3a0';
const DOWN = '#ee7b87';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isCandle(value: unknown): value is Candle {
  if (!isRecord(value)) return false;
  const { time, open, high, low, close, volume } = value;
  return (
    typeof time === 'number' && Number.isFinite(time) && time > 0 &&
    typeof open === 'number' && Number.isFinite(open) && open > 0 &&
    typeof high === 'number' && Number.isFinite(high) && high > 0 &&
    typeof low === 'number' && Number.isFinite(low) && low > 0 &&
    typeof close === 'number' && Number.isFinite(close) && close > 0 &&
    typeof volume === 'number' && Number.isFinite(volume) && volume >= 0 &&
    high >= Math.max(open, close) && low <= Math.min(open, close)
  );
}

function cleanCandles(values: Candle[]): Candle[] {
  const unique = new Map<number, Candle>();
  for (const value of values) {
    if (isCandle(value)) unique.set(Math.floor(value.time), { ...value, time: Math.floor(value.time) });
  }
  return [...unique.values()].sort((a, b) => a.time - b.time);
}

/** Explicit preview data: never called by the real-data error path. */
function previewCandles(symbol: string, interval: string): Candle[] {
  let seed = [...`${symbol}:${interval}`].reduce((n, char) => (n * 31 + char.charCodeAt(0)) >>> 0, 718);
  const random = () => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    return (seed >>> 0) / 4294967296;
  };
  const bases: Record<string, number> = {
    BTCUSDT: 68420.5, ETHUSDT: 3542.8, SOLUSDT: 172.64, BNBUSDT: 602.35,
    AVAXUSDT: 39.82, LINKUSDT: 18.46, SUIUSDT: 1.82, NEARUSDT: 6.84,
    DOGEUSDT: 0.1642, XRPUSDT: 0.5842, ADAUSDT: 0.4871, INJUSDT: 28.64,
  };
  const basePrice = bases[symbol] ?? (0.08 + (seed % 14000) / 120);
  let close = basePrice;
  const seconds = INTERVAL_SECONDS[interval] ?? 3600;
  const end = Math.floor(Date.now() / 1000 / seconds) * seconds;
  const candles = Array.from({ length: 220 }, (_, i) => {
    const open = close;
    close = open * (1 + Math.sin(i / 19) * 0.003 + (random() - 0.43) * 0.012);
    return {
      time: end - (219 - i) * seconds,
      open,
      close,
      high: Math.max(open, close) * (1 + random() * 0.008),
      low: Math.min(open, close) * (1 - random() * 0.008),
      volume: (0.3 + random()) * 1800 * (1 + Math.abs(close - open) / open * 60),
    };
  });
  const factor = basePrice / candles[candles.length - 1].close;
  return candles.map((candle) => ({
    ...candle,
    open: candle.open * factor,
    high: candle.high * factor,
    low: candle.low * factor,
    close: candle.close * factor,
  }));
}

function defaultPricePrecision(price: number): number {
  if (price >= 1000) return 2;
  if (price >= 1) return 3;
  if (price >= 0.01) return 5;
  return Math.min(14, Math.max(8, Math.ceil(-Math.log10(Math.abs(price))) + 3));
}

function tickDecimals(tick: number): number {
  const [coefficient, exponent = '0'] = tick.toString().toLowerCase().split('e');
  return Math.min(14, Math.max(0, (coefficient.split('.')[1]?.length ?? 0) - Number(exponent)));
}

function numberLabel(value: number, precision: number): string {
  return value.toLocaleString('en-US', { maximumFractionDigits: precision });
}

function timeLabel(time: number): string {
  return new Date(time).toLocaleTimeString('ar', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

export default function CandleChart({ baseUrl, market, symbol, interval, analysis, preview, tickSize, pricePrecision, onPrice }: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const latestCandleRef = useRef<Candle | null>(null);
  const hoveringRef = useRef(false);
  const onPriceRef = useRef(onPrice);
  const generationRef = useRef(0);
  const [legend, setLegend] = useState<Candle | null>(null);
  const [feed, setFeed] = useState<FeedState>('loading');
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [clock, setClock] = useState(Date.now());
  const [hasData, setHasData] = useState(false);

  useEffect(() => { onPriceRef.current = onPrice; }, [onPrice]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const chart = createChart(container, {
      width: Math.max(1, container.clientWidth),
      height: Math.max(330, container.clientHeight),
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#92a5ba',
        fontFamily: 'Nabd Arabic, Arial, sans-serif',
        fontSize: 11,
        attributionLogo: true,
      },
      grid: {
        vertLines: { color: 'rgba(132, 162, 192, 0.045)' },
        horzLines: { color: 'rgba(132, 162, 192, 0.06)' },
      },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.22 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 5, barSpacing: 7 },
      crosshair: {
        vertLine: { color: '#53677f', style: LineStyle.Dashed, labelBackgroundColor: '#25354b' },
        horzLine: { color: '#53677f', style: LineStyle.Dashed, labelBackgroundColor: '#25354b' },
      },
      localization: { locale: 'en-US' },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      borderVisible: false,
      priceLineColor: '#8dabb7',
      priceLineStyle: LineStyle.Dotted,
    });
    const volumes = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volumes.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 }, visible: false });
    chartRef.current = chart;
    candleSeriesRef.current = candles;
    volumeSeriesRef.current = volumes;

    chart.subscribeCrosshairMove((event) => {
      const value = event.seriesData.get(candles);
      const volume = event.seriesData.get(volumes);
      if (value && 'open' in value && 'high' in value && 'low' in value && 'close' in value && typeof event.time === 'number') {
        hoveringRef.current = true;
        setLegend({
          time: event.time,
          open: value.open,
          high: value.high,
          low: value.low,
          close: value.close,
          volume: volume && 'value' in volume ? volume.value : 0,
        });
      } else {
        hoveringRef.current = false;
        setLegend(latestCandleRef.current);
      }
    });

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry && chartRef.current === chart) {
        chart.resize(Math.max(1, Math.round(entry.contentRect.width)), Math.max(330, Math.round(entry.contentRect.height)));
      }
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      priceLinesRef.current = [];
      chart.remove();
    };
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const generation = ++generationRef.current;
    const controller = new AbortController();
    const current = () => generationRef.current === generation && !controller.signal.aborted;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let reconnectDelay = 1000;
    let fitted = false;
    let fetching = false;
    let streamActive = false;
    let latestStreamCandle: Candle | null = null;
    let latestStreamReceivedAt = 0;
    let data: Candle[] = [];

    latestCandleRef.current = null;
    hoveringRef.current = false;
    setLegend(null);
    setHasData(false);
    setError(null);
    setLastUpdated(null);
    setFeed(preview ? 'preview' : 'loading');
    candleSeriesRef.current?.setData([]);
    volumeSeriesRef.current?.setData([]);

    const display = (values: Candle[]) => {
      if (!current() || values.length === 0) return;
      const candles = candleSeriesRef.current;
      const volumes = volumeSeriesRef.current;
      if (!candles || !volumes) return;
      data = values;
      const last = values[values.length - 1];
      const validTick = typeof tickSize === 'number' && Number.isFinite(tickSize) && tickSize > 0;
      const precision = validTick ? tickDecimals(tickSize) : typeof pricePrecision === 'number' && Number.isInteger(pricePrecision) && pricePrecision >= 0 ? Math.min(14, pricePrecision) : defaultPricePrecision(last.close);
      candles.applyOptions({ priceFormat: { type: 'price', precision, minMove: validTick ? tickSize : 10 ** -precision } });
      candles.setData(values.map(({ time, open, high, low, close }) => ({ time: time as UTCTimestamp, open, high, low, close })));
      volumes.setData(values.map(({ time, volume, open, close }) => ({
        time: time as UTCTimestamp,
        value: volume,
        color: close >= open ? 'rgba(39, 211, 160, 0.25)' : 'rgba(238, 123, 135, 0.25)',
      })));
      latestCandleRef.current = last;
      if (!hoveringRef.current) setLegend(last);
      setHasData(true);
      onPriceRef.current?.(last.close);
      if (!fitted) {
        chartRef.current?.timeScale().fitContent();
        fitted = true;
      }
    };

    const connect = () => {
      if (!current() || preview || data.length === 0 || socket) return;
      const endpoint = market === 'spot' ? 'wss://stream.binance.com:9443/ws/' : 'wss://fstream.binance.com/ws/';
      try {
        const next = new WebSocket(`${endpoint}${symbol.toLowerCase()}@kline_${interval}`);
        socket = next;
        next.onmessage = (event: MessageEvent<unknown>) => {
          if (!current() || socket !== next || typeof event.data !== 'string') return;
          try {
            const payload: unknown = JSON.parse(event.data);
            if (!isRecord(payload) || !isRecord(payload.k)) return;
            const kline = payload.k;
            if (kline.s !== symbol || kline.i !== interval) return;
            const candle: Candle = {
              time: Number(kline.t) / 1000,
              open: Number(kline.o),
              high: Number(kline.h),
              low: Number(kline.l),
              close: Number(kline.c),
              volume: Number(kline.v),
            };
            if (!isCandle(candle) || candle.time < (latestCandleRef.current?.time ?? 0)) return;
            candleSeriesRef.current?.update({
              time: candle.time as UTCTimestamp,
              open: candle.open,
              high: candle.high,
              low: candle.low,
              close: candle.close,
            });
            volumeSeriesRef.current?.update({
              time: candle.time as UTCTimestamp,
              value: candle.volume,
              color: candle.close >= candle.open ? 'rgba(39, 211, 160, 0.25)' : 'rgba(238, 123, 135, 0.25)',
            });
            if (data[data.length - 1]?.time === candle.time) data[data.length - 1] = candle;
            else data.push(candle);
            latestCandleRef.current = candle;
            latestStreamCandle = candle;
            latestStreamReceivedAt = Date.now();
            reconnectDelay = 1000;
            streamActive = true;
            if (!hoveringRef.current) setLegend(candle);
            setLastUpdated(latestStreamReceivedAt);
            setFeed('live');
            setError(null);
            onPriceRef.current?.(candle.close);
          } catch {
            // A malformed stream event cannot replace verified market candles.
          }
        };
        next.onclose = () => {
          if (!current() || socket !== next) return;
          socket = null;
          streamActive = false;
          setFeed(data.length ? 'polling' : 'offline');
          reconnectTimer = setTimeout(connect, reconnectDelay);
          reconnectDelay = Math.min(reconnectDelay * 2, 30000);
        };
        next.onerror = () => {
          if (current() && socket === next) next.close();
        };
      } catch {
        if (current()) setFeed(data.length ? 'polling' : 'offline');
      }
    };

    const refresh = async () => {
      if (!current() || fetching) return;
      fetching = true;
      const requestStartedAt = Date.now();
      try {
        const received = await fetchCandles(baseUrl, market, symbol, interval, controller.signal);
        if (!current()) return;
        const cleaned = cleanCandles(received);
        if (cleaned.length === 0) throw new Error('لم تصل شموع صحيحة من مصدر السوق');
        if (latestStreamCandle && latestStreamReceivedAt >= requestStartedAt) {
          const matching = cleaned.findIndex((candle) => candle.time === latestStreamCandle?.time);
          if (matching >= 0) cleaned[matching] = latestStreamCandle;
          else if (latestStreamCandle.time > cleaned[cleaned.length - 1].time) cleaned.push(latestStreamCandle);
        }
        display(cleaned);
        setLastUpdated(Date.now());
        setError(null);
        streamActive = streamActive && Date.now() - latestStreamReceivedAt < 35000;
        setFeed(streamActive ? 'live' : 'polling');
        connect();
      } catch (cause: unknown) {
        if (!current()) return;
        if (!streamActive) {
          setFeed('offline');
          setError(cause instanceof DOMException && cause.name === 'AbortError'
            ? 'انتهت مهلة الاتصال بمصدر الشموع'
            : cause instanceof Error ? cause.message.slice(0, 140) : 'تعذر الاتصال بمصدر الشموع');
        }
      } finally {
        fetching = false;
      }
    };

    if (preview) {
      display(previewCandles(symbol, interval));
      setLastUpdated(null);
    } else {
      void refresh();
      pollTimer = setInterval(() => { void refresh(); }, 15000);
    }

    return () => {
      controller.abort();
      ++generationRef.current;
      if (pollTimer) clearInterval(pollTimer);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (socket) {
        socket.onmessage = null;
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
    };
  }, [baseUrl, market, symbol, interval, preview, tickSize, pricePrecision]);

  const analysisValid = Boolean(analysis && analysis.validUntil > clock);

  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series) return;
    for (const line of priceLinesRef.current) series.removePriceLine(line);
    priceLinesRef.current = [];
    if (!analysis || !analysisValid || analysis.symbol !== symbol || analysis.market !== market || analysis.side === 'wait') return;
    const levels = [
      { price: analysis.entry, title: 'دخول', color: '#88b5ff' },
      { price: analysis.stop, title: 'وقف', color: DOWN },
      ...analysis.targets.slice(0, 3).map((price, index) => ({ price, title: `هدف ${index + 1}`, color: UP })),
    ];
    priceLinesRef.current = levels
      .filter((level) => Number.isFinite(level.price) && level.price > 0)
      .map((level) => series.createPriceLine({ ...level, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true }));
  }, [analysis, symbol, market, analysisValid]);

  const stale = !preview && lastUpdated !== null && clock - lastUpdated > 35000;
  const change = legend ? legend.close - legend.open : 0;
  const legendPrecision = typeof tickSize === 'number' && Number.isFinite(tickSize) && tickSize > 0 ? tickDecimals(tickSize) : typeof pricePrecision === 'number' && Number.isInteger(pricePrecision) && pricePrecision >= 0 ? Math.min(14, pricePrecision) : defaultPricePrecision(legend?.close ?? 1);
  const statusLabel = preview ? 'بيانات توضيحية · غير قابلة للتداول'
    : stale ? 'البيانات متأخرة'
      : feed === 'live' ? 'مباشر'
        : feed === 'polling' ? 'تحديث دوري'
          : feed === 'loading' ? 'جارٍ الاتصال بالسوق' : 'الاتصال منقطع';

  return (
    <div className="chart-shell">
      <div className="chart-legend" dir="ltr">
        <strong className="chart-legend-symbol">{symbol.replace(/USDT$/, '/USDT')}</strong>
        {legend && <>
          <span className="chart-price" style={{ color: change >= 0 ? UP : DOWN }}>{numberLabel(legend.close, legendPrecision)}</span>
          <span className="chart-legend-item"><small>O</small> {numberLabel(legend.open, legendPrecision)}</span>
          <span className="chart-legend-item"><small>H</small> {numberLabel(legend.high, legendPrecision)}</span>
          <span className="chart-legend-item"><small>L</small> {numberLabel(legend.low, legendPrecision)}</span>
          <span className="chart-legend-item"><small>V</small> {legend.volume.toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 1 })}</span>
        </>}
      </div>
      <div className="chart-stage" style={{ position: 'relative' }}>
        <div ref={containerRef} className="chart-canvas" dir="ltr" style={{ width: '100%', height: 'clamp(330px, 45vh, 460px)' }} />
        {!hasData && <div className="chart-overlay" dir="rtl" role="status">
          <span className={feed === 'loading' ? 'chart-loader' : 'chart-empty-icon'} aria-hidden="true">{feed === 'loading' ? '◌' : '◇'}</span>
          <strong>{feed === 'loading' ? 'جارٍ تحميل الشموع' : 'لا توجد بيانات سوق متاحة'}</strong>
          {error && <span>{error}</span>}
        </div>}
      </div>
      <div className={`chart-status${stale || feed === 'offline' ? ' chart-status-error' : ''}`} dir="rtl" role="status" aria-live="polite">
        <span className={`chart-live-badge${preview ? ' chart-preview-badge' : ''}`}>
          <i aria-hidden="true" />{statusLabel}
        </span>
        {!preview && lastUpdated && <span>آخر تحديث {timeLabel(lastUpdated)}</span>}
        {hasData && error && !preview && <span className="chart-status-message">{error}</span>}
        {preview && <span>معاينة التصميم</span>}
      </div>
    </div>
  );
}
