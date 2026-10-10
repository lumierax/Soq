import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { armBot, closePosition, configureBot, fetchAnalysis, fetchBacktest, fetchBot, fetchMarket, pauseBot } from './api';
import { startLocalMarket, stopLocalMarket } from './local-market';
import type { Analysis, BacktestResult, BotConfig, BotMode, BotPosition, BotStatus, Horizon, Market, MarketItem, MarketSnapshot, Side } from './types';
import CandleChart from './components/CandleChart';
import Icon from './components/Icon';

type View = Market | 'bot';
const SIDE_LABEL: Record<Side, string> = { long: 'شراء', short: 'بيع', wait: 'انتظار' };
const DEFAULT_CONFIG: BotConfig = { riskPercent: 1, maxPositions: 3, maxDailyLossPercent: 4, leverage: 3, minScore: 75, minRiskReward: 2, capital: 1000 };
const FRAME_LABEL: Record<string, string> = { '1w': 'أسبوع', '1d': 'يوم', '4h': '4 ساعات', '1h': 'ساعة', '30m': '30 دقيقة', '15m': '15 دقيقة', '5m': '5 دقائق', '3m': '3 دقائق' };
const SYMBOL_COLORS: Record<string, string> = { BTC: '#f2aa4b', ETH: '#a6addf', SOL: '#92d9c3', BNB: '#edc563', AVAX: '#e27982', LINK: '#799ce8', SUI: '#71bdec', NEAR: '#e5e6e5', DOGE: '#c6a859', XRP: '#bbc6da', ADA: '#70a5ed', INJ: '#87d6dc' };

function readSaved(key: string, fallback: string): string {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function save(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* Browser storage can be unavailable. */ } }
function number(value: number | null | undefined, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value) : '—';
}
function price(value: number | null | undefined, tickSize?: number, precision?: number) {
  let digits = !value ? 2 : value >= 100 ? 2 : value >= 1 ? 3 : value >= .01 ? 4 : Math.min(14, Math.max(8, Math.ceil(-Math.log10(Math.abs(value))) + 3));
  if (typeof tickSize === 'number' && Number.isFinite(tickSize) && tickSize > 0) {
    const [coefficient, exponent = '0'] = tickSize.toString().toLowerCase().split('e');
    digits = Math.min(14, Math.max(0, (coefficient.split('.')[1]?.length ?? 0) - Number(exponent)));
  } else if (typeof precision === 'number' && Number.isInteger(precision) && precision >= 0) digits = Math.min(14, precision);
  if (typeof value === 'number' && value !== 0 && Math.abs(value) < 1e-8) {
    digits = Math.max(digits, Math.min(14, Math.ceil(-Math.log10(Math.abs(value))) + 3));
    if (Math.abs(value) < 5e-15) return value.toExponential(4);
  }
  return number(value, digits);
}
function signed(value: number | null | undefined, suffix = '%') { return typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${number(value)}${suffix}` : '—'; }
function compact(value: number | null | undefined) { return typeof value === 'number' ? new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value) : '—'; }
function errorText(error: unknown) { return error instanceof Error ? error.message : 'تعذر إكمال العملية.'; }
function isAbort(error: unknown) { return error instanceof DOMException && error.name === 'AbortError'; }
function validAnalysis(analysis: Analysis | null | undefined, now: number) { return !!analysis && analysis.validUntil > now && Number.isFinite(analysis.entry); }
function relativeTime(timestamp: number, now: number) {
  if (!timestamp) return 'لم يتصل بعد';
  const seconds = Math.max(0, Math.floor((now - timestamp) / 1000));
  return seconds < 5 ? 'الآن' : seconds < 60 ? `منذ ${seconds} ث` : `منذ ${Math.floor(seconds / 60)} د`;
}
function dateLabel(timestamp: number) { return new Date(timestamp).toLocaleDateString('ar', { month: 'short', day: 'numeric', year: 'numeric' }); }

function previewSnapshot(market: Market, horizon: Horizon): MarketSnapshot {
  const now = Date.now();
  const samples = [
    ['BTC', 68420.5, 2.84, 892000000, 88], ['ETH', 3542.8, 4.21, 526000000, 91], ['SOL', 172.64, 6.72, 286000000, 86],
    ['BNB', 602.35, 1.45, 156000000, 79], ['AVAX', 39.82, 5.16, 88000000, 82], ['LINK', 18.46, 3.68, 75000000, 76],
    ['SUI', 1.82, -2.32, 68000000, 84], ['NEAR', 6.84, 4.12, 64000000, 73], ['DOGE', .1642, -1.08, 58000000, 62],
    ['XRP', .5842, .94, 52000000, 58], ['ADA', .4871, -1.46, 35000000, 54], ['INJ', 28.64, 2.38, 28000000, 77],
  ] as const;
  return {
    market, horizon, updatedAt: now, source: 'binance', connection: 'offline', totalSymbols: samples.length, analyzedSymbols: samples.length, scanProgress: 100, btcRegime: 'bullish', error: null,
    items: samples.map(([baseAsset, value, change24h, quoteVolume, score], i) => {
      const side: Side = score < 70 ? 'wait' : market === 'futures' && i === 6 ? 'short' : 'long';
      const direction = side === 'short' ? -1 : 1;
      const entry = value * (side === 'short' ? 1.004 : .996);
      return {
        symbol: `${baseAsset}USDT`, baseAsset, quoteAsset: 'USDT', pricePrecision: value >= 100 ? 2 : value >= 1 ? 3 : 4, quantityPrecision: 3, tickSize: value >= 100 ? .01 : value >= 1 ? .001 : .0001, stepSize: .001, minQty: .001, minNotional: 5,
        price: value, change24h, quoteVolume, high24h: value * 1.027, low24h: value * .956, fundingRate: .0001,
        analysis: { symbol: `${baseAsset}USDT`, market, horizon, asOf: now, validUntil: now + 86400000, side, score, entry, stop: entry * (1 - direction * .024), targets: [.044, .072, .108].map(x => entry * (1 + direction * x)), riskReward: 1.83, atrPercent: 2.4, trend: side === 'short' ? 'bearish' : 'bullish', reasons: ['اتفاق اتجاه الفريمات الرئيسية', side === 'short' ? 'زخم هابط مع سيولة مرتفعة' : 'زخم صاعد مع سيولة مرتفعة', side === 'short' ? 'منطقة دخول قريبة من مقاومة فنية' : 'منطقة دخول قريبة من دعم فني'], warnings: ['تحليل توضيحي؛ ليس توصية تداول.'], timeframes: (market === 'spot' ? horizon === 'investment' ? ['1w', '1d', '4h'] : ['1d', '4h'] : ['1h', '15m', '3m']).map(interval => ({ interval, trend: side === 'short' ? 'bearish' : 'bullish', rsi: 58.6, adx: 31.4 })), strategyVersion: '1.0', closedCandleTime: now - 3600000 },
      };
    }),
  };
}

function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    ref.current?.focus();
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'Tab') {
        const elements = Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, [tabindex="0"]') ?? []);
        const first = elements[0]; const last = elements[elements.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', handle);
    return () => { document.body.style.overflow = oldOverflow; document.removeEventListener('keydown', handle); previous?.focus(); };
  }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="modal-card" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}>
      <div className="panel-title"><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="إغلاق"><Icon name="close" /></button></div>{children}
    </div>
  </div>;
}

function Stat({ label, value, suffix, icon, tone = '', detail }: { label: string; value: ReactNode; suffix?: string; icon: string; tone?: string; detail?: string }) {
  return <div className="stat-card"><span className="stat-icon"><Icon name={icon} /></span><div><span className="metric-label">{label}</span><div className={`metric-value ${tone}`} dir="ltr">{value}{suffix && <small>{suffix}</small>}</div>{detail && <span className="stat-detail">{detail}</span>}</div></div>;
}

function AssetIcon({ asset }: { asset: string }) {
  return <span className={`coin-icon coin-${asset.toLowerCase()}`} style={{ color: SYMBOL_COLORS[asset] ?? '#9aaebc', background: `${SYMBOL_COLORS[asset] ?? '#9aaebc'}16` }}>{asset === 'BTC' ? '₿' : asset === 'ETH' ? 'Ξ' : asset === 'SOL' ? '≋' : asset.slice(0, 1)}</span>;
}

function Settings({ initialBase, token, onSave, onClose }: { initialBase: string; token: string; onSave: (base: string, token: string) => void; onClose: () => void }) {
  const [connection, setConnection] = useState<'direct' | 'server'>(initialBase ? 'server' : 'direct');
  const [base, setBase] = useState(initialBase);
  const [secret, setSecret] = useState(token);
  const [error, setError] = useState('');
  return <Modal title="إعداد الاتصال" onClose={onClose}><form className="settings-panel" onSubmit={event => {
    event.preventDefault();
    if (connection === 'direct') { onSave('', ''); return; }
    const clean = base.trim().replace(/\/+$/, '');
    if (!clean) { setError('أدخل رابط جهاز تشغيل البوت.'); return; }
    if (clean) {
      try {
        const url = new URL(clean);
        if (url.username || url.password || url.search || url.hash || !['http:', 'https:'].includes(url.protocol)) throw new Error();
        if (/(^|\.)example\.(com|net|org)$/.test(url.hostname)) { setError('هذا رابط مثال. أدخل رابط جهازك الفعلي.'); return; }
        if (url.protocol !== 'https:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) { setError('استخدم HTTPS لحماية اتصال البوت.'); return; }
      } catch { setError('أدخل رابط HTTPS صالحًا لجهاز تشغيل البوت.'); return; }
    }
    onSave(clean, secret.trim());
  }}>
    <div className="segmented-control connection-toggle" role="group" aria-label="طريقة الاتصال"><button type="button" className={connection === 'direct' ? 'active' : ''} aria-pressed={connection === 'direct'} onClick={() => { setConnection('direct'); setError(''); }}><Icon name="pulse" />مباشر من Binance</button><button type="button" className={connection === 'server' ? 'active' : ''} aria-pressed={connection === 'server'} onClick={() => { setConnection('server'); setError(''); }}><Icon name="bot" />خادم البوت</button></div>
    {connection === 'direct' ? <div className="credential-note"><Icon name="check" /><span>الشموع والشارت والتحليل مباشرة. لا تحتاج رابط خادم أو مفتاح API.</span></div> : <>
    <label className="field-label">رابط جهاز تشغيل البوت<input className="input-field" dir="ltr" value={base} onChange={e => setBase(e.target.value)} placeholder="https://" autoComplete="url" inputMode="url" /></label>
    <p className="muted field-hint">عنوان HTTPS لجهازك. رابط GitHub Pages ليس خادم البوت.</p>
    <label className="field-label">رمز وصول البوت <span className="muted">APP_TOKEN</span><input className="input-field" dir="ltr" type="password" value={secret} onChange={e => setSecret(e.target.value)} placeholder="رمز الوصول من الخادم" autoComplete="off" /></label>
    <div className="credential-note"><Icon name="shield" /><span>الرمز لهذه الجلسة فقط. مفاتيح Binance وتليجرام تبقى على جهاز البوت.</span></div></>}
    {error && <div className="form-error" role="alert">{error}</div>}
    <button className="action-button primary" type="submit"><Icon name="check" />حفظ الاتصال</button>
  </form></Modal>;
}

function Backtest({ baseUrl, market, symbol, horizon, preview }: { baseUrl: string; market: Market; symbol: string; horizon: Horizon; preview: boolean }) {
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  useEffect(() => { controller.current?.abort(); setResult(null); setError(''); setBusy(false); return () => controller.current?.abort(); }, [baseUrl, market, symbol, horizon, preview]);
  const run = async () => {
    controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError('');
    try { const data = await fetchBacktest(baseUrl, market, symbol, horizon, abort.signal); if (!abort.signal.aborted) setResult(data); }
    catch (failure) { if (!abort.signal.aborted) setError(errorText(failure)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  };
  return <div className="backtest-panel">
    <div className="panel-title"><div className="inline-title"><Icon name="flask" /><h3>اختبار تاريخي</h3></div><button className="action-button secondary compact" disabled={busy || preview} onClick={run}>{busy ? <Icon name="refresh" className="spinning" /> : <Icon name="play" />}{busy ? 'جارٍ الاختبار' : result ? 'إعادة الاختبار' : 'اختبر الإعداد'}</button></div>
    {!result && !error && <p className="muted">{preview ? 'يتاح الاختبار عند الاتصال ببيانات السوق.' : 'شموع مغلقة، مع احتساب الرسوم. الأداء السابق لا يضمن النتائج.'}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {result && <><div className="backtest-stats">
      <div><span className="metric-label">الصفقات</span><strong dir="ltr">{result.tradeCount}</strong></div>
      <div><span className="metric-label">العائد</span><strong className={result.netReturn >= 0 ? 'positive' : 'negative'} dir="ltr">{signed(result.netReturn)}</strong></div>
      <div><span className="metric-label">أقصى تراجع</span><strong className="negative" dir="ltr">{number(result.maxDrawdown)}%</strong></div>
      <div><span className="metric-label">نسبة الفوز</span><strong dir="ltr">{result.winRate === null ? '—' : `${number(result.winRate, 1)}%`}</strong></div>
      <div><span className="metric-label">معامل الربح</span><strong dir="ltr">{number(result.profitFactor)}</strong></div>
    </div><p className="muted backtest-period">{dateLabel(result.sampleStart)} — {dateLabel(result.sampleEnd)} · رسوم {result.feesBps} نقطة أساس</p><div className="research-notes">{result.limitations.map((note, i) => <p key={i}>{note}</p>)}</div></>}
  </div>;
}

function SignalDetail({ item, analysis, now, preview }: { item: MarketItem; analysis: Analysis | null; now: number; preview: boolean }) {
  const fresh = validAnalysis(analysis, now);
  if (!analysis) return <div className="signal-detail empty-analysis"><Icon name="clock" /><div><h3>التحليل قيد الإعداد</h3><p className="muted">تظهر الفرصة عند اكتمال فحص الشموع والسيولة.</p></div></div>;
  const tradeable = fresh && analysis.side !== 'wait';
  return <div className="signal-detail">
    <div className="panel-title"><div className="inline-title"><Icon name="grid" /><h3>خطة {item.baseAsset}</h3><span className={`signal-pill ${fresh ? analysis.side : 'wait'}`}>{fresh ? SIDE_LABEL[analysis.side] : 'منتهية الصلاحية'}</span></div><span className="analysis-timestamp muted"><Icon name="clock" />{preview ? 'نموذج توضيحي' : relativeTime(analysis.asOf, now)}</span></div>
    <div className="details-grid">
      <div className="detail-card strength-card"><span className="metric-label">قوة الإشارة <span className="muted">/ 100</span></span><div className="strength-value"><strong dir="ltr">{number(analysis.score, 0)}</strong><span className="strength-track"><span style={{ width: `${analysis.score}%` }} /></span></div><span className="muted">تقييم فني، وليس احتمال ربح</span></div>
      <div className="detail-card"><span className="metric-label">العائد / المخاطرة</span><strong dir="ltr">{number(analysis.riskReward)}<small>R</small></strong><span className="muted">إلى الهدف الأول</span></div>
      <div className="detail-card"><span className="metric-label">توافق الفريمات</span><div className="timeframe-trends">{analysis.timeframes.map(frame => <span key={frame.interval} className={frame.trend === 'bullish' ? 'positive' : frame.trend === 'bearish' ? 'negative' : 'muted'}><Icon name={frame.trend === 'neutral' ? 'clock' : 'arrow'} className={frame.trend === 'bearish' ? 'rotate-down' : ''} /><b dir="ltr">{frame.interval}</b></span>)}</div><span className="muted">شموع مغلقة فقط</span></div>
    </div>
    {tradeable && <div className="setup-levels">
      <div className="level-card entry"><span>الدخول المرجعي</span><strong dir="ltr">{price(analysis.entry, item.tickSize, item.pricePrecision)}</strong><small>USDT</small></div>
      <div className="level-card stop"><span>وقف الخسارة</span><strong dir="ltr">{price(analysis.stop, item.tickSize, item.pricePrecision)}</strong><small dir="ltr">{number(Math.abs(analysis.stop / analysis.entry - 1) * 100)}%</small></div>
      {analysis.targets.slice(0, 3).map((target, i) => <div className="level-card target" key={i}><span>الهدف {i + 1}</span><strong dir="ltr">{price(target, item.tickSize, item.pricePrecision)}</strong><small dir="ltr">{number(Math.abs(target / analysis.entry - 1) * 100)}%</small></div>)}
    </div>}
    {!tradeable && <div className="wait-note"><Icon name="eye" /><span>{fresh ? 'لا توجد نقطة دخول مكتملة الآن. العملة تحت المراقبة.' : 'يلزم تحليل حديث قبل اتخاذ قرار.'}</span></div>}
    <div className="signal-reasons">{analysis.reasons.slice(0, 3).map((reason, i) => <span key={i}><Icon name="check" />{reason}</span>)}</div>
    {analysis.warnings.length > 0 && <div className="analysis-warnings">{analysis.warnings.slice(0, 3).map((warning, i) => <span key={i}><Icon name="info" />{warning}</span>)}</div>}
  </div>;
}

function BotPanel({ baseUrl, token, preview, onSettings }: { baseUrl: string; token: string; preview: boolean; onSettings: () => void }) {
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [draft, setDraft] = useState<BotConfig>(DEFAULT_CONFIG);
  const [mode, setMode] = useState<BotMode>('paper');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmLive, setConfirmLive] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const [closing, setClosing] = useState<BotPosition | null>(null);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const scope = useRef(0);
  const statusVersion = useRef(0);
  const actionPending = useRef(false);
  const initialized = useRef(false);
  const statusLabel = { stopped: 'متوقف', running: 'يعمل', paused: 'موقوف مؤقتًا', error: 'يحتاج مراجعة' };
  useEffect(() => { dirtyRef.current = dirty; }, [dirty]);
  useEffect(() => { scope.current += 1; statusVersion.current += 1; actionPending.current = false; initialized.current = false; setBot(null); setError(''); setNotice(''); setDirty(false); dirtyRef.current = false; setBusy(false); setConfirmLive(false); setClosing(null); }, [baseUrl, token]);
  useEffect(() => {
    if (!baseUrl || !token) return;
    const abort = new AbortController(); let closed = false; let pending = false;
    const load = async () => {
      if (pending || actionPending.current) return; pending = true; setLoading(true);
      const version = statusVersion.current;
      try { const value = await fetchBot(baseUrl, token, abort.signal); if (!closed && version === statusVersion.current && !actionPending.current) { setBot(value); if (!dirtyRef.current) setDraft(value.config); if (!initialized.current || value.state === 'running') setMode(value.mode); initialized.current = true; setError(''); } }
      catch (failure) { if (!closed && version === statusVersion.current && !isAbort(failure)) setError(errorText(failure)); }
      finally { if (!closed) setLoading(false); pending = false; }
    };
    void load(); const interval = window.setInterval(load, bot?.state === 'running' ? 3000 : 12000);
    return () => { closed = true; abort.abort(); window.clearInterval(interval); };
  }, [baseUrl, token, bot?.state, refreshTick]);
  const perform = async (action: () => Promise<BotStatus>, message: string, onSuccess?: (value: BotStatus) => void) => {
    const currentScope = scope.current;
    statusVersion.current += 1; actionPending.current = true;
    setBusy(true); setError(''); setNotice('');
    try { const value = await action(); if (currentScope === scope.current) { setBot(value); setMode(value.mode); onSuccess?.(value); setNotice(message); setConfirmLive(false); setClosing(null); setConfirmation(''); } }
    catch (failure) { if (currentScope === scope.current) setError(errorText(failure)); }
    finally { if (currentScope === scope.current) { actionPending.current = false; setBusy(false); setRefreshTick(value => value + 1); } }
  };
  const configFields: { key: keyof BotConfig; label: string; unit: string; min: number; max: number; step: number }[] = [
    { key: 'capital', label: 'رأس المال المخصص', unit: 'USDT', min: 50, max: 1000000, step: 10 },
    { key: 'riskPercent', label: 'المخاطرة لكل صفقة', unit: '%', min: .1, max: 3, step: .1 },
    { key: 'maxPositions', label: 'الصفقات المتزامنة', unit: 'صفقات', min: 1, max: 10, step: 1 },
    { key: 'leverage', label: 'الرافعة المالية', unit: '×', min: 1, max: 10, step: 1 },
    { key: 'minScore', label: 'أقل قوة إشارة', unit: '/100', min: 70, max: 100, step: 1 },
    { key: 'minRiskReward', label: 'أقل عائد / مخاطرة', unit: 'R', min: 1.8, max: 10, step: .1 },
    { key: 'maxDailyLossPercent', label: 'حد الخسارة اليومية', unit: '%', min: .5, max: 15, step: .5 },
  ];
  const connected = !!bot && !error;
  const liveReady = connected && bot.liveAllowed && bot.credentialsConfigured;
  const canArm = !!token && connected && !preview && !dirty && !busy && (mode !== 'live' || liveReady) && bot?.state !== 'running';
  const configLocked = bot?.state === 'running' || !!bot?.positions.length;
  return <>
    <div className="section-heading"><div><div className="eyebrow"><span className="eyebrow-line" />FUTURES AUTOMATION</div><h1>البوت. تحت سيطرتك.</h1><p>تداول فيوتشر آلي، بإعدادات وحدود واضحة.</p></div><div className={`bot-state badge ${bot?.state === 'running' ? 'positive' : 'neutral'}`}><span className={`status-dot ${bot?.state === 'running' ? 'is-live' : ''}`} />{bot ? statusLabel[bot.state] : 'غير متصل'}</div></div>
    {preview && <div className="connection-banner preview-banner"><Icon name="flask" /><span>وضع العرض التوضيحي. تشغيل البوت معطل.</span></div>}
    {(!baseUrl || !token) && <div className="connection-banner"><Icon name="shield" /><span>{!baseUrl ? 'شغّل البوت على جهاز يعمل باستمرار، واربطه هنا. إشعارات الصفقات تصل إلى تليجرام.' : 'أدخل رمز وصول جهازك لإدارة البوت.'}</span><button onClick={onSettings} className="action-button secondary compact">إعداد الاتصال</button></div>}
    {error && <div className="connection-banner error-banner" role="alert"><Icon name="info" /><span>{error}</span><button className="icon-button" aria-label="إعادة الاتصال" onClick={() => setRefreshTick(x => x + 1)} disabled={loading}><Icon name="refresh" className={loading ? 'spinning' : ''} /></button></div>}
    {notice && <div className="connection-banner success-banner" role="status"><Icon name="check" /><span>{notice}</span></div>}
    {bot && bot.mode !== 'paper' && bot.dailyBaselineComplete === false && !!bot.dailyBaselineAt && <div className="connection-banner"><Icon name="clock" /><span>مرجع الخسارة منذ آخر اتصال ({new Date(bot.dailyBaselineAt).toLocaleString('ar', { timeZone: 'UTC', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })} UTC)؛ بداية اليوم غير مكتملة.</span></div>}
    <div className="stats-strip bot-stats">
      <Stat label={bot?.mode === 'paper' ? 'رصيد المحاكاة' : 'حقوق الحساب'} value={number(bot?.equity)} suffix="USDT" icon="wallet" detail={bot?.mode === 'paper' ? 'محاكاة محلية، لا أموال حقيقية' : bot?.mode === 'testnet' ? 'حساب اختبار Binance' : 'من الحساب المتصل'} />
      <Stat label={bot?.mode !== 'paper' && bot?.dailyBaselineComplete === false ? 'النتيجة منذ الاتصال' : 'نتيجة اليوم'} value={signed(bot?.dailyPnl, '')} suffix="USDT" icon="trend" tone={(bot?.dailyPnl ?? 0) >= 0 ? 'positive' : 'negative'} />
      <Stat label="الصفقات المفتوحة" value={bot ? bot.positions.length : '—'} icon="grid" detail="فيوتشر فقط" />
      <Stat label="مفاتيح Binance" value={bot ? bot.credentialsConfigured ? 'جاهزة' : 'غير مضافة' : '—'} icon="shield" detail="محفوظة على الخادم" />
    </div>
    <div className="bot-layout">
      <section className="bot-hero">
        <div className="bot-illustration"><Icon name="bot" /><span className={`bot-orbit ${bot?.state === 'running' ? 'active' : ''}`} /></div>
        <div className="eyebrow">NABD AUTOPILOT</div><h2>الفرصة تتحول إلى تنفيذ.</h2><p className="muted">دخول من إشارات حديثة، وقف حماية، ومراقبة مستمرة على الخادم.</p>
        <div className="mode-picker segmented-control" role="group" aria-label="وضع التداول">{(['paper', 'testnet', 'live'] as BotMode[]).map(value => <button className={mode === value ? 'active' : ''} key={value} disabled={busy || bot?.state === 'running'} onClick={() => setMode(value)}>{value === 'paper' ? 'محاكاة' : value === 'testnet' ? 'اختبار Binance' : 'تداول حقيقي'}{value === 'live' && <span className="mode-live-dot" />}</button>)}</div>
        <div className={`mode-note ${mode === 'live' ? 'negative' : 'muted'}`}><Icon name={mode === 'live' ? 'info' : 'flask'} /><span>{mode === 'paper' ? 'تنفيذ ورقي ببيانات السوق؛ لا تُرسل أوامر.' : mode === 'testnet' ? 'أوامر فعلية على بيئة اختبار Binance.' : 'أوامر حقيقية من أموال حسابك.'}</span></div>
        {mode === 'live' && !liveReady && <p className="form-error">يلزم تفعيل ALLOW_LIVE_TRADING ومفاتيح التداول على الخادم.</p>}
        {dirty && <p className="form-error">احفظ الإعدادات قبل تشغيل البوت.</p>}
        <div className="bot-actions"><button className="action-button primary" disabled={!canArm} onClick={() => { if (mode === 'live') { setConfirmLive(true); setConfirmation(''); } else void perform(() => armBot(baseUrl, token, mode), mode === 'paper' ? 'بدأت المحاكاة.' : 'بدأ التداول على بيئة الاختبار.'); }}><Icon name={busy ? 'refresh' : 'play'} className={busy ? 'spinning' : ''} />{bot?.state === 'running' ? 'البوت يعمل' : 'تشغيل البوت'}</button><button className="action-button secondary" disabled={!token || busy || bot?.state !== 'running'} onClick={() => void perform(() => pauseBot(baseUrl, token), 'توقف فتح صفقات جديدة. تبقى المراكز الحالية تحت الحماية.')}><Icon name="pause" />إيقاف الدخول</button></div>
        <div className="bot-safety"><span><Icon name="shield" />هامش معزول</span><span><Icon name="check" />وقف حماية</span><span><Icon name="clock" />متابعة مستمرة</span></div>
        {bot?.telegram && <p className={`small-note ${bot.telegram.error ? 'negative' : 'muted'}`}>إشعارات تليجرام: {bot.telegram.error ? 'تعذر الإرسال — راجع إعدادات جهازك' : bot.telegram.configured && bot.telegram.running ? `مفعلة · ${bot.telegram.delivered} مُرسل` : 'غير معدّة'}</p>}
        <p className="muted small-note">إيقاف الدخول يبقي الصفقات المفتوحة. الإغلاق من قائمة المراكز.</p>
      </section>
      <section className="bot-config"><div className="panel-title"><h3>إعدادات التنفيذ</h3><span className="badge neutral">{dirty ? 'تغييرات غير محفوظة' : 'حدود المخاطرة'}</span></div>
        <form onSubmit={event => { event.preventDefault(); void perform(() => configureBot(baseUrl, token, draft), 'تم حفظ إعدادات البوت.', value => { setDraft(value.config); setDirty(false); dirtyRef.current = false; }); }}><div className="config-grid">{configFields.map(field => <label className="field-label" key={field.key}>{field.label}<div className="input-with-unit"><input className="input-field" type="number" dir="ltr" required min={field.min} max={field.max} step={field.step} value={draft[field.key]} disabled={busy || !token || configLocked} onChange={event => { setDraft(previous => ({ ...previous, [field.key]: event.target.value === '' ? 0 : Number(event.target.value) })); setDirty(true); dirtyRef.current = true; }} /><span>{field.unit}</span></div></label>)}</div>{configLocked && <p className="muted small-note">أوقف الدخول وأغلق المراكز لتعديل الحدود.</p>}<button className="action-button secondary" type="submit" disabled={!token || !connected || busy || !dirty || configLocked}><Icon name="check" />حفظ الإعدادات</button></form>
      </section>
    </div>
    <section className="positions-panel"><div className="panel-title"><div className="inline-title"><h3>المراكز المفتوحة</h3><span className="count-badge">{bot?.positions.length ?? '—'}</span></div><button className="icon-button" aria-label="تحديث المراكز" disabled={!token || loading} onClick={() => setRefreshTick(x => x + 1)}><Icon name="refresh" className={loading ? 'spinning' : ''} /></button></div>
      {!bot?.positions.length ? <div className="empty-state compact-empty"><Icon name="wallet" /><p>{!token ? 'اربط البوت لعرض المراكز.' : loading ? 'جارٍ الاتصال بالحساب…' : 'لا توجد مراكز مفتوحة.'}</p></div> : <div className="position-list">{bot.positions.map(position => <div className="position-row" key={position.id}><div className="coin-cell"><AssetIcon asset={position.symbol.replace(/USDT$/, '')} /><div><b dir="ltr">{position.symbol}</b><span className={`signal-pill ${position.side === 'short' ? 'short' : 'long'}`}>{position.side === 'short' ? 'بيع' : 'شراء'} · {position.mode === 'paper' ? 'محاكاة' : position.mode === 'testnet' ? 'اختبار' : 'حقيقي'}</span></div></div><div><span className="metric-label">الدخول</span><strong dir="ltr">{price(position.entry)}</strong></div><div><span className="metric-label">السعر</span><strong dir="ltr">{price(position.markPrice)}</strong></div><div><span className="metric-label">الوقف</span><strong className="negative" dir="ltr">{price(position.stop)}</strong></div><div><span className="metric-label">النتيجة</span><strong className={position.pnl >= 0 ? 'positive' : 'negative'} dir="ltr">{signed(position.pnl, '')}<small> USDT</small></strong></div><button className="action-button danger compact" disabled={busy || !token} onClick={() => setClosing(position)}>إغلاق المركز</button></div>)}</div>}
    </section>
    <section className="events-panel"><div className="panel-title"><h3>آخر النشاط</h3><span className="muted">من سجل الخادم</span></div>{!bot?.events.length ? <p className="muted">لا يوجد نشاط مسجل.</p> : <div className="event-list">{bot.events.slice(-8).reverse().map(event => <div className="event-row" key={event.id}><span className={`event-dot ${event.type.includes('error') ? 'negative' : ''}`} /><span>{event.message}</span><time dir="ltr">{new Date(event.time).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}</time></div>)}</div>}</section>
    {confirmLive && <Modal title="تفعيل التداول الحقيقي" onClose={() => { if (!busy) setConfirmLive(false); }}><div className="live-confirmation"><div className="confirmation-icon negative"><Icon name="info" /></div><p>سيُرسل البوت أوامر فيوتشر حقيقية إلى حسابك. راجع رأس المال والرافعة وحد الخسارة.</p><div className="confirmation-summary"><span>رأس المال <b dir="ltr">{number(draft.capital)} USDT</b></span><span>الرافعة <b dir="ltr">{draft.leverage}×</b></span><span>حد الخسارة اليومية <b dir="ltr">{draft.maxDailyLossPercent}%</b></span></div><label className="field-label">اكتب العبارة للتفعيل<input className="input-field" dir="ltr" value={confirmation} onChange={e => setConfirmation(e.target.value)} placeholder="ENABLE LIVE TRADING" autoComplete="off" /></label>{error && <p className="form-error">{error}</p>}<button className="action-button danger" disabled={confirmation !== 'ENABLE LIVE TRADING' || busy || preview || !liveReady} onClick={() => void perform(() => armBot(baseUrl, token, 'live', confirmation), 'تم تفعيل التداول الحقيقي.')}><Icon name="play" />{busy ? 'جارٍ التحقق من الحساب…' : 'تأكيد التداول الحقيقي'}</button></div></Modal>}
    {closing && <Modal title="إغلاق المركز" onClose={() => { if (!busy) setClosing(null); }}><div className="live-confirmation"><p>إغلاق مركز <b dir="ltr">{closing.symbol}</b> بالكامل بسعر السوق{closing.mode === 'live' ? ' من حسابك الحقيقي' : closing.mode === 'paper' ? ' في المحاكاة' : ' في بيئة الاختبار'}.</p><p className="muted">قد تختلف النتيجة الفعلية عن القيمة الظاهرة.</p>{error && <p className="form-error">{error}</p>}<button className="action-button danger" disabled={busy} onClick={() => void perform(() => closePosition(baseUrl, token, closing.id), 'تم تنفيذ طلب إغلاق المركز.')}><Icon name="close" />{busy ? 'جارٍ الإغلاق…' : 'تأكيد الإغلاق'}</button></div></Modal>}
  </>;
}

export default function App() {
  const [view, setView] = useState<View>('spot');
  const market: Market = view === 'futures' ? 'futures' : 'spot';
  const [spotHorizon, setSpotHorizon] = useState<Horizon>('swing');
  const horizon: Horizon = market === 'spot' ? spotHorizon : 'intraday';
  const [baseUrl, setBaseUrl] = useState(() => { const saved = readSaved('nabd.apiBase', ''); try { return /(^|\.)example\.(com|net|org)$/.test(new URL(saved).hostname) ? '' : saved; } catch { return ''; } });
  const [token, setToken] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const [snapshot, setSnapshot] = useState<MarketSnapshot | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [preview, setPreview] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [selectedSymbol, setSelectedSymbol] = useState('BTCUSDT');
  const [interval, setIntervalValue] = useState('4h');
  const [search, setSearch] = useState('');
  const [direction, setDirection] = useState<'all' | Side>('all');
  const [minScore, setMinScore] = useState(0);
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [favorites, setFavorites] = useState<string[]>(() => { try { const saved: unknown = JSON.parse(readSaved('nabd.favorites', '[]')); return Array.isArray(saved) ? saved.filter((item): item is string => typeof item === 'string') : []; } catch { return []; } });
  const [selectedAnalysis, setSelectedAnalysis] = useState<Analysis | null>(null);
  const [livePrice, setLivePrice] = useState<number | null>(null);
  const chartPanelRef = useRef<HTMLElement | null>(null);
  const selectSymbol = (symbol: string) => {
    setSelectedSymbol(symbol);
    if (window.matchMedia('(max-width: 800px)').matches) {
      window.requestAnimationFrame(() => chartPanelRef.current?.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }));
    }
  };

  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  useEffect(() => { setIntervalValue(market === 'spot' ? '4h' : '15m'); setDirection('all'); setSelectedAnalysis(null); setLivePrice(null); }, [market]);
  useEffect(() => { save('nabd.favorites', JSON.stringify(favorites)); }, [favorites]);
  useEffect(() => { if (!baseUrl && !preview) startLocalMarket(); else stopLocalMarket(); return stopLocalMarket; }, [baseUrl, preview]);
  useEffect(() => {
    if (preview) { setLoading(false); return; }
    const abort = new AbortController(); let closed = false; let pending = false;
    setSnapshot(null); setSelectedAnalysis(null); setError(''); setLoading(true);
    const load = async () => {
      if (pending || document.visibilityState === 'hidden') return; pending = true;
      try { const result = await fetchMarket(baseUrl, market, horizon, abort.signal); if (!closed) { setSnapshot(result); setError(result.error ?? ''); } }
      catch (failure) { if (!closed && !isAbort(failure)) setError(errorText(failure)); }
      finally { pending = false; if (!closed) setLoading(false); }
    };
    const visible = () => { if (document.visibilityState === 'visible') void load(); };
    void load(); const timer = window.setInterval(load, 12000); document.addEventListener('visibilitychange', visible);
    return () => { closed = true; abort.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [baseUrl, market, horizon, preview, refreshTick]);

  const demonstration = useMemo(() => previewSnapshot(market, horizon), [market, horizon]);
  const activeSnapshot = preview ? demonstration : snapshot?.market === market && snapshot.horizon === horizon ? snapshot : null;
  const items = activeSnapshot?.items ?? [];
  useEffect(() => {
    if (items.length && !items.some(item => item.symbol === selectedSymbol)) setSelectedSymbol(items[0].symbol);
  }, [items, selectedSymbol]);
  const selectedItem = items.find(item => item.symbol === selectedSymbol) ?? null;
  useEffect(() => {
    setSelectedAnalysis(null); setLivePrice(null);
    if (preview || !selectedItem) return;
    const abort = new AbortController(); let closed = false; let pending = false;
    const load = async () => {
      if (pending) return; pending = true;
      try { const data = await fetchAnalysis(baseUrl, market, selectedSymbol, horizon, abort.signal); if (!closed) setSelectedAnalysis(data); }
      catch { /* The market item still carries its last verified analysis. */ }
      finally { pending = false; }
    };
    void load(); const timer = window.setInterval(load, 30000);
    return () => { closed = true; abort.abort(); window.clearInterval(timer); };
  }, [baseUrl, market, horizon, selectedSymbol, preview, !!selectedItem]);
  const analysis = preview ? selectedItem?.analysis ?? null : selectedAnalysis && selectedAnalysis.symbol === selectedSymbol && selectedAnalysis.market === market && selectedAnalysis.horizon === horizon && (!selectedItem?.analysis || selectedAnalysis.asOf >= selectedItem.analysis.asOf) ? selectedAnalysis : selectedItem?.analysis ?? null;
  const toggleFavorite = (symbol: string) => setFavorites(previous => previous.includes(symbol) ? previous.filter(item => item !== symbol) : [...previous, symbol]);
  const filtered = useMemo(() => {
    const term = search.trim().toUpperCase().replace(/[\s/\-]/g, '');
    return items.filter(item => (!term || item.symbol.includes(term) || item.baseAsset.includes(term)) && (!favoritesOnly || favorites.includes(item.symbol)) && (direction === 'all' || (validAnalysis(item.analysis, now) ? item.analysis?.side : 'wait') === direction) && (!minScore || (validAnalysis(item.analysis, now) && (item.analysis?.score ?? 0) >= minScore)))
      .sort((a, b) => (validAnalysis(b.analysis, now) ? b.analysis?.score ?? 0 : 0) - (validAnalysis(a.analysis, now) ? a.analysis?.score ?? 0 : 0) || (b.quoteVolume ?? 0) - (a.quoteVolume ?? 0));
  }, [items, search, favoritesOnly, favorites, direction, minScore, now]);
  const readySignals = items.filter(item => validAnalysis(item.analysis, now) && item.analysis?.side !== 'wait');
  const hasAnalyses = items.some(item => validAnalysis(item.analysis, now));
  const hasTickers = items.some(item => typeof item.quoteVolume === 'number' && Number.isFinite(item.quoteVolume));
  const hasPriceChanges = items.some(item => typeof item.change24h === 'number' && Number.isFinite(item.change24h));
  const positiveCount = items.filter(item => (item.change24h ?? 0) > 0).length;
  const totalVolume = items.reduce((sum, item) => sum + (item.quoteVolume ?? 0), 0);
  const realConnection = !preview && !error && activeSnapshot?.connection === 'live' && !!activeSnapshot.updatedAt && now - activeSnapshot.updatedAt < 90000;
  const frameOptions = market === 'spot' ? ['4h', '1d', '1w'] : ['3m', '5m', '15m', '30m', '1h'];
  const showSettings = () => setSettingsOpen(true);
  const navigate = (next: View) => { setView(next); setSearch(''); setFavoritesOnly(false); };
  const resetFilters = () => { setSearch(''); setDirection('all'); setMinScore(0); setFavoritesOnly(false); };

  return <div className="app-shell" dir="rtl">
    <header className="app-header"><a href="#" className="brand" onClick={event => { event.preventDefault(); navigate('spot'); }} aria-label="نبض - الرئيسية"><span className="brand-mark"><Icon name="pulse" /></span><span className="brand-copy"><strong>نبض<span className="brand-dot">.</span></strong><small>NABD TERMINAL</small></span></a><div className="header-controls"><div className={`live-status ${realConnection ? 'connected' : preview ? 'demo' : 'disconnected'}`}><span className={`status-dot ${realConnection ? 'is-live' : ''}`} /><span>{preview ? 'عرض توضيحي' : realConnection ? 'السوق مباشر' : loading ? 'جارٍ الاتصال' : 'غير متصل'}</span><span className="status-exchange">BINANCE</span></div><span className="header-divider" /><button className="icon-button settings-trigger" onClick={showSettings} aria-label="إعدادات الاتصال"><Icon name="settings" /></button><span className="profile-avatar">N</span></div></header>
    <nav className="top-nav" aria-label="الأقسام الرئيسية"><div className="nav-items">{([{ id: 'spot', label: 'سبوت', icon: 'coins', caption: 'SPOT' }, { id: 'futures', label: 'فيوتشر', icon: 'bolt', caption: 'FUTURES' }, { id: 'bot', label: 'بوت التداول', icon: 'bot', caption: 'AUTOPILOT' }] as const).map(tab => <button className={`nav-item ${view === tab.id ? 'active' : ''}`} aria-current={view === tab.id ? 'page' : undefined} key={tab.id} onClick={() => navigate(tab.id)}><Icon name={tab.icon} /><span>{tab.label}</span><small>{tab.caption}</small>{tab.id === 'bot' && <span className="nav-new-dot" />}</button>)}</div><span className="nav-caption">رؤية شاملة. قرارات مدروسة.</span></nav>
    <main className="app-main">
      {view === 'bot' ? <BotPanel baseUrl={baseUrl} token={token} preview={preview} onSettings={showSettings} /> : <>
        <div className="section-heading"><div><div className="eyebrow"><span className="eyebrow-line" />{market === 'spot' ? 'SPOT INTELLIGENCE' : 'FUTURES INTELLIGENCE'}</div><h1>{market === 'spot' ? 'السوق، بوضوح.' : 'إيقاع أسرع. رؤية أعمق.'}<span className="heading-accent">{market === 'spot' ? ' فرصتك التالية هنا.' : ' ابقَ متقدمًا.'}</span></h1><p>{market === 'spot' ? 'استثمار وسوينج. نظرة شاملة على كل أزواج USDT.' : 'إشارات شراء وبيع. من الساعة إلى ثلاث دقائق.'}</p></div>{market === 'spot' ? <div className="horizon-toggle segmented-control" role="group" aria-label="أسلوب التحليل"><button className={spotHorizon === 'swing' ? 'active' : ''} onClick={() => setSpotHorizon('swing')}><Icon name="trend" />سوينج</button><button className={spotHorizon === 'investment' ? 'active' : ''} onClick={() => setSpotHorizon('investment')}><Icon name="shield" />استثمار</button></div> : <button className="action-button secondary" onClick={() => navigate('bot')}><Icon name="bot" />إدارة البوت<Icon name="arrow" /></button>}</div>
        {preview ? <div className="connection-banner preview-banner"><Icon name="flask" /><span><b>عرض توضيحي</b> · جميع الأسعار والتوصيات هنا اصطناعية. البوت معطل.</span><button className="action-button secondary compact" onClick={() => setPreview(false)}>العودة للسوق</button></div> : error && <div className="connection-banner error-banner" role="status"><Icon name="info" /><span>{error}</span><button className="action-button secondary compact" onClick={() => setRefreshTick(x => x + 1)}>إعادة المحاولة</button>{baseUrl && <button className="action-button secondary compact" onClick={showSettings}>إعداد الاتصال</button>}{!items.length && <button className="action-button secondary compact" onClick={() => setPreview(true)}><Icon name="eye" />معاينة التصميم</button>}</div>}
        <div className="stats-strip">
          <Stat label="أزواج USDT" value={activeSnapshot?.totalSymbols ? activeSnapshot.totalSymbols : '—'} icon="coins" detail={preview ? 'عينة توضيحية فقط' : 'كل أزواج Binance المتاحة'} />
          <Stat label="فرص مكتملة" value={hasAnalyses ? readySignals.length : '—'} icon="bolt" tone="positive" detail={market === 'spot' ? 'توصيات فقط' : 'شراء وبيع'} />
          <Stat label="حجم التداول · 24 ساعة" value={hasTickers ? compact(totalVolume) : '—'} suffix="USDT" icon="chart" detail={hasPriceChanges ? `${positiveCount} عملة صاعدة` : 'في انتظار البيانات'} />
          <Stat label="فحص السوق" value={activeSnapshot?.totalSymbols ? `${number(Math.min(100, activeSnapshot.scanProgress), 0)}%` : '—'} icon="shield" detail={activeSnapshot?.totalSymbols ? `${activeSnapshot.analyzedSymbols} / ${activeSnapshot.totalSymbols} تحليلاً` : 'يتواصل في الخلفية'} />
        </div>
        <div className="market-layout">
          <section className="watchlist-panel"><div className="panel-title"><div className="inline-title"><h2>رادار الفرص</h2><span className="count-badge">{filtered.length}</span></div><div className="inline-actions"><button className={`icon-button ${favoritesOnly ? 'active' : ''}`} onClick={() => setFavoritesOnly(previous => !previous)} aria-pressed={favoritesOnly} aria-label={favoritesOnly ? 'عرض جميع العملات' : 'عرض المفضلة'} title="المفضلة"><Icon name="star" /></button><button className="icon-button" disabled={loading || preview} aria-label="تحديث السوق" onClick={() => setRefreshTick(x => x + 1)}><Icon name="refresh" className={loading ? 'spinning' : ''} /></button></div></div>
            <label className="search-field"><Icon name="search" /><input dir="rtl" type="search" placeholder="ابحث عن عملة…" aria-label="البحث عن كل أزواج USDT" value={search} onChange={event => setSearch(event.target.value)} /><span className="search-hint" dir="ltr">USDT</span></label>
            <div className="toolbar"><div className="signal-filters"><button className={`filter-button ${direction === 'all' ? 'active' : ''}`} onClick={() => setDirection('all')}>الكل</button><button className={`filter-button ${direction === 'long' ? 'active' : ''}`} onClick={() => setDirection('long')}>شراء</button>{market === 'futures' && <button className={`filter-button ${direction === 'short' ? 'active' : ''}`} onClick={() => setDirection('short')}>بيع</button>}<button className={`filter-button ${direction === 'wait' ? 'active' : ''}`} onClick={() => setDirection('wait')}>مراقبة</button></div><button title="تصفية قوة الإشارة 75 فأكثر" className={`filter-button score-filter ${minScore ? 'active' : ''}`} aria-pressed={!!minScore} onClick={() => setMinScore(value => value ? 0 : 75)}><Icon name="bolt" /><span dir="ltr">75+</span></button></div>
            <div className="list-column-labels"><span>العملة / السعر</span><span>القوة / الاتجاه</span></div>
            <div className="market-table">{filtered.length ? filtered.map((item, index) => {
              const fresh = validAnalysis(item.analysis, now); const side = fresh ? item.analysis!.side : 'wait';
              return <div className={`market-row ${selectedSymbol === item.symbol ? 'selected' : ''}`} key={item.symbol}><button className="market-row-select" onClick={() => selectSymbol(item.symbol)} aria-label={`عرض تحليل ${item.baseAsset}`} aria-pressed={selectedSymbol === item.symbol}><span className="coin-cell"><AssetIcon asset={item.baseAsset} /><span><span className="coin-name" dir="ltr">{item.baseAsset}<small>/USDT</small>{index === 0 && fresh && <span className="top-pick-dot" title="أعلى قوة إشارة" />}</span><span className="coin-symbol price-cell" dir="ltr">{price(item.price, item.tickSize, item.pricePrecision)}<span className={(item.change24h ?? 0) >= 0 ? 'positive' : 'negative'}>{signed(item.change24h)}</span></span></span></span><span className="row-signal"><span className={`signal-score ${fresh && item.analysis!.score >= 75 ? 'strong' : ''}`} dir="ltr">{fresh ? number(item.analysis!.score, 0) : '—'}<small>/100</small></span><span className={`signal-pill ${side}`}>{SIDE_LABEL[side]}</span></span></button><button className={`row-favorite icon-button ${favorites.includes(item.symbol) ? 'active' : ''}`} aria-label={`${favorites.includes(item.symbol) ? 'حذف' : 'إضافة'} ${item.baseAsset} ${favorites.includes(item.symbol) ? 'من' : 'إلى'} المفضلة`} onClick={() => toggleFavorite(item.symbol)}><Icon name="star" /></button></div>;
            }) : <div className="empty-state radar-empty"><div className="empty-radar"><Icon name={loading ? 'refresh' : 'search'} className={loading ? 'spinning' : ''} /></div><h3>{loading ? 'نقرأ نبض السوق…' : items.length ? 'لا توجد نتائج' : 'بانتظار اتصال السوق'}</h3><p>{loading ? 'جلب أزواج USDT وتحليل الشموع.' : items.length ? 'جرّب تغيير البحث أو الفلاتر.' : 'تظهر العملات والأسعار بعد اتصال الخادم بـ Binance.'}</p>{items.length ? <button className="action-button secondary compact" onClick={resetFilters}>مسح الفلاتر</button> : !loading && <button className="action-button secondary compact" onClick={() => setPreview(true)}><Icon name="eye" />معاينة توضيحية</button>}</div>}</div>
            <div className="watchlist-footer"><span className="status-dot" /><span>{preview ? 'بيانات اصطناعية' : `${relativeTime(activeSnapshot?.updatedAt ?? 0, now)} · ترتيب حسب قوة الإشارة`}</span>{activeSnapshot && !preview && <span className="scan-mini" style={{ '--progress': `${activeSnapshot.scanProgress}%` } as React.CSSProperties} />}</div>
          </section>
          <section className="chart-panel" ref={chartPanelRef}>
            {selectedItem ? <><div className="chart-header"><div className="coin-cell"><AssetIcon asset={selectedItem.baseAsset} /><div><h2 dir="ltr">{selectedItem.baseAsset}<span className="muted"> / USDT</span><span className="badge neutral">{market === 'spot' ? 'SPOT' : 'PERP'}</span></h2><span className="muted">{market === 'spot' ? horizon === 'investment' ? 'نظرة استثمارية' : 'نظرة سوينج' : 'عقد دائم · USDT'}</span></div></div><div className="chart-quote"><strong dir="ltr">{price(livePrice ?? selectedItem.price, selectedItem.tickSize, selectedItem.pricePrecision)}</strong><span className={(selectedItem.change24h ?? 0) >= 0 ? 'positive' : 'negative'} dir="ltr">{signed(selectedItem.change24h)}<small> 24h</small></span></div></div>
              <div className="chart-toolbar"><div className="timeframe-picker segmented-control" role="group" aria-label="فريم الشارت">{frameOptions.map(frame => <button key={frame} className={interval === frame ? 'active' : ''} aria-pressed={interval === frame} onClick={() => setIntervalValue(frame)}>{FRAME_LABEL[frame]}</button>)}</div><div className="chart-type-label"><Icon name="chart" /><span>شموع يابانية</span></div></div>
              <div className="chart-container"><CandleChart key={`${market}/${selectedSymbol}/${preview}`} baseUrl={baseUrl} market={market} symbol={selectedSymbol} interval={interval} analysis={analysis} preview={preview} tickSize={selectedItem.tickSize} pricePrecision={selectedItem.pricePrecision} onPrice={setLivePrice} /></div>
              <div className="chart-market-stats"><span>أعلى 24h <b dir="ltr">{price(selectedItem.high24h, selectedItem.tickSize, selectedItem.pricePrecision)}</b></span><span>أدنى 24h <b dir="ltr">{price(selectedItem.low24h, selectedItem.tickSize, selectedItem.pricePrecision)}</b></span><span>حجم 24h <b dir="ltr">{compact(selectedItem.quoteVolume)}</b></span>{market === 'futures' && <span>التمويل <b dir="ltr">{typeof selectedItem.fundingRate === 'number' ? `${number(selectedItem.fundingRate * 100, 4)}%` : '—'}</b></span>}</div>
              <SignalDetail item={selectedItem} analysis={analysis} now={now} preview={preview} />
              <Backtest baseUrl={baseUrl} market={market} symbol={selectedSymbol} horizon={horizon} preview={preview} />
            </> : <div className="empty-state chart-empty"><div className="empty-chart-art"><span /><span /><span /><span /><span /><span /><Icon name="pulse" /></div><span className="eyebrow">YOUR NEXT MOVE</span><h2>كل فرصة تبدأ برؤية واضحة.</h2><p>شارت تفاعلي وخطة تداول موجزة لكل عملة.<br />{baseUrl ? 'في انتظار اتصال جهازك بالسوق.' : 'اتصال مباشر ببيانات Binance العامة.'}</p><div className="inline-actions"><button className="action-button primary" onClick={() => setRefreshTick(x => x + 1)}><Icon name="refresh" />إعادة المحاولة</button><button className="action-button secondary" onClick={() => setPreview(true)}><Icon name="eye" />معاينة التصميم</button></div><span className="muted small-note">المعاينة بيانات اصطناعية، وليست توصيات.</span></div>}
          </section>
        </div>
        <div className="market-footnote"><span><Icon name="shield" />الأسعار العامة بلا مفتاح API. {market === 'spot' ? 'السبوت توصيات فقط.' : 'تشغيل التداول من قسم البوت.'}</span><span>قوة الإشارة تقييم فني. لا ضمان للأرباح.</span></div>
      </>}
    </main>
    <footer className="app-footer"><span className="footer-brand">NABD<span> / </span>نبض</span><span>اقرأ السوق. اختر بوعي.</span><span className="footer-version">v1.0 · Binance USDT</span></footer>
    <nav className="bottom-nav" aria-label="التنقل السريع">{([{ id: 'spot', label: 'سبوت', icon: 'coins' }, { id: 'futures', label: 'فيوتشر', icon: 'bolt' }, { id: 'bot', label: 'البوت', icon: 'bot' }] as const).map(tab => <button key={tab.id} className={view === tab.id ? 'active' : ''} onClick={() => navigate(tab.id)} aria-current={view === tab.id ? 'page' : undefined}><Icon name={tab.icon} /><span>{tab.label}</span></button>)}<button onClick={showSettings}><Icon name="settings" /><span>الإعدادات</span></button></nav>
    {settingsOpen && <Settings initialBase={baseUrl} token={token} onClose={closeSettings} onSave={(base, value) => { setBaseUrl(base); setToken(value); save('nabd.apiBase', base); setSettingsOpen(false); setRefreshTick(x => x + 1); }} />}
  </div>;
}
