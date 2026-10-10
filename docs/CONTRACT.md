# NABD implementation contract

Python 3.12 / FastAPI backend, React TypeScript / Vite frontend, Capacitor iOS shell.

Preserve the complete original strategy when changing transport or hosting. `backend/app/analysis.py` is the reference; the TypeScript engine must produce matching outputs for identical inputs. Keep every indicator, timeframe, score weight, threshold, BTC/funding context, entry/stop/target formula and strategy-defined `validUntil`. Do not add recommendation filters or shorten validity in the transport adapter or UI. Market-data availability checks and request scheduling do not replace or simplify the strategy.

All times: Unix milliseconds. All prices/numerics: JSON numbers. Market types: `spot`, `futures`. Horizons: `swing`, `investment` for spot, `intraday` for futures. Symbol is Binance's exact symbol including USDT.

## Market service (`app/market.py`, `app/analysis.py`)

`MarketService()`; async `start()`, `stop()`, `refresh_universe()`, `snapshot(market, horizon)`, `candles(market,symbol,interval,limit=500)`, `analyze_symbol(market,symbol,horizon)`. Public `signals` mapping `(market,horizon,symbol)` -> analysis dict. `symbols` mapping market -> list of metadata dicts. `status()` returns dict.

Snapshots contain `market`, `horizon`, `updatedAt`, `source: "binance"`, `connection: "live"|"degraded"|"offline"`, `totalSymbols`, `analyzedSymbols`, `scanProgress`, `btcRegime`, `items`, `error` (nullable). ALL active USDT spot pairs / perpetual futures are in items, including unanalysed ones. No fake market data. Metadata: `{symbol,baseAsset,quoteAsset,pricePrecision,quantityPrecision,tickSize,stepSize,minQty,minNotional}`. Items: metadata plus `{price,change24h,quoteVolume,high24h,low24h,fundingRate?,analysis: Analysis|null}`.

Analysis: `{symbol,market,horizon,asOf,validUntil,side:"long"|"short"|"wait",score:0..100,entry,stop,targets:[number,number,number],riskReward,atrPercent,trend:"bullish"|"bearish"|"neutral",reasons:[Arabic strings],warnings:[Arabic strings],timeframes:[{interval,trend,rsi,adx}],strategyVersion:"1.0",closedCandleTime}`. Score is a transparent heuristic setup strength, NOT success probability. No guarantees, no invented performance. Reject stale/missing/insufficient candles. Use closed candles only. Investment: 1w/1d/4h. Swing: 1d/4h. Futures: 1h/15m/3m. Candles: `[{time:seconds,open,high,low,close,volume}]`.

## Bot (`app/bot.py`, `app/storage.py`)

`BotService(market)`; async `start()`, `stop()`, `status()`, `configure(dict)`, `arm(mode, confirmation)`, `pause()`, `close_position(position_id)`; `history()` async returns list. Service reads env for keys, never sends keys to UI, never logs them. Env: `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `ALLOW_LIVE_TRADING=false`, `BOT_DB_PATH`. Futures only; isolated margin, one-way mode (reject hedge mode), exchange filters, idempotency, durable ledger, reduce-only exit/stop, verified exchange protection, kill switch. Mode `paper|testnet|live`. Cannot arm live without `ALLOW_LIVE_TRADING=true`, configured credentials, validated exchange account, and explicit confirmation `ENABLE LIVE TRADING`. Paper trading visibly labeled. Restart must reconcile positions and disarm. No arbitrary auto execution at startup. Market scanning continues while bot stopped.

Bot config: `{riskPercent:1,maxPositions:3,maxDailyLossPercent:4,leverage:3,minScore:75,minRiskReward:2,capital:1000}`. Status: `{state:"stopped"|"running"|"paused"|"error",mode,credentialsConfigured,liveAllowed,config,equity,dailyPnl,positions:[{id,symbol,side,entry,markPrice,quantity,stop,target,pnl,openedAt,mode}],events:[{id,time,type,message}],error}`. `arm` returns status. Config and stop paths safe regardless mode.

## HTTP root integration

GET `/api/health`, `/api/markets?market=spot&horizon=swing`, `/api/candles?market=spot&symbol=BTCUSDT&interval=4h`, `/api/analysis?market=spot&symbol=BTCUSDT&horizon=swing`, `/api/bot`, `/api/bot/history`; POST `/api/bot/config` JSON config, `/api/bot/arm` `{mode,confirmation}`, `/api/bot/pause`, `/api/bot/close/{id}`. Bearer auth required for ALL bot routes using `APP_TOKEN`; default server only localhost with generated token file. No credentials in browser. Public market routes anonymous. GET `/api/research` lists strategy limitations; GET `/api/backtest` on-demand closed-candle historical simulation (root).

Empty frontend API base selects direct public Binance REST and local TypeScript analysis/backtesting; native iOS uses CapacitorHttp, web uses fetch, with no fake fallback. An explicit backend URL retains the server API path and enables optional bot management. Pages hosts frontend/dist static files only; it does not run Python or keep a bot alive. Bot access token held in session memory unless native keychain added. Never Binance or Telegram secrets in frontend. Direct Binance websocket supplements candles with validated timestamps. Chart intervals spot `4h,1d,1w`; futures `3m,5m,15m,30m,1h`. Lightweight Charts bundled. App title: `نبض` / `NABD`. Premium midnight navy, jade green, subtle bronze, Arabic RTL, quiet glass panels, no busy gimmicks. Use tabbed primary nav, mobile bottom nav. Frontend TSX owns content/components; CSS independent file styles all common class names.
