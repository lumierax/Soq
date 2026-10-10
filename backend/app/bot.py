"""Futures-only executor. The exchange stop, not the UI, is the protection.

Current Binance conditional-order documentation (checked 2026-10-10):
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#new-algo-order
Run only ONE backend process against a ledger / Binance account. Market-order
responses that are uncertain are queried, never blindly submitted again.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from contextlib import suppress
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode

import httpx

from .storage import Ledger, now_ms
from .telegram import TelegramNotifier

DEFAULT_CONFIG = dict(riskPercent=1, maxPositions=3, maxDailyLossPercent=4,
                      leverage=3, minScore=75, minRiskReward=2, capital=1000)
TAKER_FEE = Decimal('0.0005')  # Conservative estimate; exchange fills replace this in live history.
SLIPPAGE = Decimal('0.001')
DAY_MS = 86_400_000


class BotError(ValueError):
    pass


class ExchangeError(BotError):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(f'Binance ({code}): {message}')


def D(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BotError('قيمة رقمية غير صالحة') from None
    if not result.is_finite():
        raise BotError('القيمة الرقمية يجب أن تكون محدودة')
    return result


def decimal_text(value: Decimal) -> str:
    return format(value, 'f')


def round_step(value: Decimal, step: Decimal, up: bool = False) -> Decimal:
    if step <= 0:
        raise BotError('فلاتر كمية أو سعر غير صالحة')
    return (value / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step


class BinanceFutures:
    def __init__(self, mode: str, key: str, secret: str):
        self.mode = mode
        self.base = 'https://testnet.binancefuture.com' if mode == 'testnet' else 'https://fapi.binance.com'
        self.key = key
        self.secret = secret
        self.offset = 0
        self.client = httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def request(self, method: str, path: str, params: dict | None = None, *, signed: bool = True):
        params = dict(params or {})
        headers = {}
        if signed:
            params.update(timestamp=now_ms() + self.offset, recvWindow=5000)
            encoded = urlencode(params)  # Percent-encode BEFORE signing, including Unicode symbols.
            signature = hmac.new(self.secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
            query = encoded + '&signature=' + signature
            headers['X-MBX-APIKEY'] = self.key
        else:
            query = urlencode(params)
        try:
            response = await self.client.request(method, self.base + path + ('?' + query if query else ''), headers=headers)
        except httpx.HTTPError:
            # Never include the request URL: it contains a signature and possibly account information.
            raise ExchangeError(0, 'تعذر الاتصال بالمنصة؛ نتيجة الطلب غير مؤكدة') from None
        try:
            data = response.json()
        except ValueError:
            raise ExchangeError(response.status_code, 'استجابة غير صالحة من المنصة') from None
        if response.is_error or (isinstance(data, dict) and int(data.get('code', 0)) < 0):
            message = str(data.get('msg', 'رفضت المنصة الطلب')) if isinstance(data, dict) else 'رفضت المنصة الطلب'
            raise ExchangeError(int(data.get('code', response.status_code)) if isinstance(data, dict) else response.status_code, message)
        return data

    async def sync_clock(self):
        before = now_ms()
        data = await self.request('GET', '/fapi/v1/time', signed=False)
        self.offset = int(data['serverTime']) - (before + now_ms()) // 2
        if abs(self.offset) > 60_000:
            raise BotError('ساعة الخادم غير متزامنة؛ صححها قبل التداول')

    async def close(self):
        await self.client.aclose()


class BotService:
    def __init__(self, market):
        self.market = market
        self.ledger = Ledger(os.getenv('BOT_DB_PATH', 'data/bot.sqlite3'))
        self.config = self.ledger.get('config', DEFAULT_CONFIG.copy())
        self.mode = self.ledger.get('mode', 'paper')
        self.state = 'stopped'
        self.error = None
        self.exchange = None
        self._task = None
        self._lock = asyncio.Lock()
        self._account_equity = 0.0
        self._account_daily = 0.0
        self._available = 0.0
        self._exchange_positions = []
        self._filters = {}
        self._filters_at = 0
        self._orders = []
        self._account_at = 0
        self._account_mode = None
        self._last_protection_check = 0
        self._reconciled = False
        self._paper_marks = {}
        self.notifications = TelegramNotifier()

    def _event(self, kind, message):
        self.ledger.event(kind, message)
        # Notification delivery never participates in exchange execution.
        try:
            self.notifications.publish(kind, message, self.mode)
        except Exception:
            pass

    @property
    def credentials_configured(self):
        return bool(os.getenv('BINANCE_API_KEY') and os.getenv('BINANCE_API_SECRET'))

    @property
    def live_allowed(self):
        return os.getenv('ALLOW_LIVE_TRADING', 'false').lower() == 'true'

    async def start(self):
        if self._task and not self._task.done():
            return
        try:
            await self.notifications.start()
        except Exception:
            pass
        self.state = 'stopped'  # Deliberate: persisted state never arms an account.
        self.ledger.event('system', 'بدء الخدمة: التداول معطل حتى التفعيل الصريح')
        if any(p['mode'] != 'paper' for p in self.ledger.positions()) or self.ledger.pending_intents() or self.ledger.get('orphanProtection', []):
            try:
                if not self.credentials_configured:
                    raise BotError('مراكز سابقة تحتاج مفاتيح المنصة للمطابقة؛ التفعيل محظور')
                await self._connect(self.mode)
                await self._reconcile()
            except Exception as exc:
                self._fail(exc)
        self._task = asyncio.create_task(self._supervise(), name='nabd-futures-bot')

    async def stop(self):
        # Let a currently submitted order finish its protection / emergency-close
        # transaction before cancelling the supervisor during ordinary shutdown.
        async with self._lock:
            self.state = 'stopped'
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.exchange:
            await self.exchange.close()
            self.exchange = None
        self.ledger.event('system', 'إيقاف الخدمة؛ تبقى أوامر الحماية المؤكدة على المنصة')
        try:
            await self.notifications.stop()
        except Exception:
            pass

    async def configure(self, values: dict):
        async with self._lock:
            if self.state == 'running' or self.ledger.positions() or self.ledger.pending_intents():
                raise BotError('أوقف الدخول وأغلق المراكز قبل تعديل إعدادات المخاطر')
            if not isinstance(values, dict) or set(values) - set(DEFAULT_CONFIG):
                raise BotError('إعداد غير معروف')
            merged = {**self.config, **values}
            bounds = dict(riskPercent=(0.1, 5), maxPositions=(1, 10), maxDailyLossPercent=(0.5, 20),
                          leverage=(1, 10), minScore=(60, 100), minRiskReward=(1.5, 10), capital=(10, 1e8))
            for key, (low, high) in bounds.items():
                value = D(merged[key])
                if not low <= value <= high or isinstance(merged[key], bool):
                    raise BotError(f'قيمة {key} خارج النطاق المسموح')
                if key in ('leverage', 'maxPositions') and value != value.to_integral_value():
                    raise BotError(f'{key} يجب أن يكون عدداً صحيحاً')
                merged[key] = int(value) if key in ('leverage', 'maxPositions') else float(value)
            if merged['capital'] != self.config['capital']:
                baseline = self.ledger.get('daily:paper')
                if baseline and baseline['day'] == now_ms() // DAY_MS:
                    baseline['equity'] += merged['capital'] - self.config['capital']
                    self.ledger.set('daily:paper', baseline)
            self.config = merged
            self.ledger.set('config', merged)
            self.ledger.event('config', 'تم حفظ إعدادات التداول')
        return await self.status()

    async def _connect(self, mode):
        if self.exchange:
            await self.exchange.close()
        self.exchange = BinanceFutures(mode, os.environ['BINANCE_API_KEY'], os.environ['BINANCE_API_SECRET'])
        await self.exchange.sync_clock()
        data = await self.exchange.request('GET', '/fapi/v1/exchangeInfo', signed=False)
        self._filters = {s['symbol']: s for s in data['symbols']
                         if s.get('status') == 'TRADING' and s.get('quoteAsset') == 'USDT'
                         and s.get('contractType') == 'PERPETUAL'}
        self._filters_at = now_ms()
        position_mode = await self.exchange.request('GET', '/fapi/v1/positionSide/dual')
        assets_mode = await self.exchange.request('GET', '/fapi/v1/multiAssetsMargin')
        if position_mode.get('dualSidePosition') is not False:
            raise BotError('يلزم وضع اتجاه واحد في حساب الفيوتشر؛ وضع التحوط غير مدعوم')
        if assets_mode.get('multiAssetsMargin') is not False:
            raise BotError('يلزم هامش USDT أحادي الأصل؛ وضع الأصول المتعددة غير مدعوم')
        await self._refresh_account(force=True)

    async def arm(self, mode: str, confirmation: str = ''):
        async with self._lock:
            if mode not in ('paper', 'testnet', 'live'):
                raise BotError('وضع تداول غير صالح')
            if self.ledger.positions() and mode != self.mode:
                raise BotError('أغلق المراكز قبل تبديل وضع التداول')
            if self.ledger.pending_intents():
                raise BotError('توجد أوامر غير مؤكدة؛ يلزم المطابقة مع المنصة قبل التفعيل')
            if self.ledger.get('liquidation', []):
                raise BotError('تصفية قاطع الخسارة لم تكتمل؛ التفعيل محظور')
            if mode == 'live' and (not self.live_allowed or confirmation != 'ENABLE LIVE TRADING'):
                raise BotError('التداول الحقيقي يحتاج ALLOW_LIVE_TRADING=true وتأكيد ENABLE LIVE TRADING')
            if mode != 'paper' and not self.credentials_configured:
                raise BotError('مفاتيح Binance مطلوبة على الخادم فقط')
            # Account validation must finish before we persist the selected mode or enable execution.
            try:
                self.state = 'stopped'
                self.error = None
                if mode != 'paper':
                    await self._connect(mode)
                    await self._reconcile(mode)
                    if self.state == 'error':
                        raise BotError(self.error or 'فشلت مطابقة الحماية؛ التفعيل محظور')
                    if self.ledger.pending_intents() or self.ledger.get('orphanProtection', []):
                        raise BotError('تعذر حسم أوامر سابقة؛ التفعيل محظور')
                elif any(p['mode'] != 'paper' for p in self.ledger.positions()):
                    raise BotError('مراكز المنصة تحتاج المطابقة أولاً')
                self.mode = mode
                self.ledger.set('mode', mode)
                if self._daily_loss_exceeded():
                    raise BotError('تم بلوغ حد الخسارة اليومية؛ التفعيل محظور حتى يوم UTC التالي')
                self.error = None
                self.state = 'running'
                self._event('armed', 'تفعيل ' + {'paper': 'محاكاة فقط — بلا أوامر حقيقية', 'testnet': 'Binance Testnet', 'live': 'التداول الحقيقي'}[mode])
            except Exception as exc:
                self._fail(exc)
                raise
        return await self.status()

    async def pause(self):
        async with self._lock:
            self.state = 'paused'
            self._event('pause', 'توقف فتح مراكز جديدة؛ تستمر متابعة المراكز والحماية')
        return await self.status()

    async def history(self):
        return self.ledger.positions(active=False)[:250]

    def _paper_stats(self):
        positions = self.ledger.positions(mode='paper')
        raw = sum(self._raw_pnl(p) for p in positions)
        cash = self.ledger.cash_total('paper')
        today = now_ms() // DAY_MS * DAY_MS
        equity = self.config['capital'] + cash + raw
        daily = self._daily_view('paper', equity, self.ledger.cash_total('paper', today), raw)
        return equity, daily

    def _daily_view(self, mode, equity, realized_today, unrealized, transfers=0.0):
        """Rebase carry positions across UTC days; transfers never count as PNL.

        A pre-midnight valuation within 60s provides the opening mark. On first
        attachment / a long overnight outage, midnight holdings cannot be
        reconstructed from current account positions. We explicitly expose the
        observed baseline time instead of claiming that approximation is exact.
        """
        now = now_ms()
        day = now // DAY_MS
        baseline = self.ledger.get('daily:' + mode)
        if not baseline or baseline['day'] != day:
            last = self.ledger.get('lastValuation:' + mode)
            adjacent = last and last['day'] == day - 1 and now - last['at'] <= 60_000
            opening_unrealized = last['unrealized'] if adjacent else unrealized
            initial_pnl = realized_today + unrealized - opening_unrealized
            baseline = dict(day=day, equity=equity - initial_pnl - transfers,
                            unrealized=opening_unrealized, asOf=last['at'] if adjacent else now,
                            midnightObserved=bool(adjacent) or (unrealized == 0))
            self.ledger.set('daily:' + mode, baseline)
        self.ledger.set('lastValuation:' + mode, dict(day=day, at=now, unrealized=unrealized))
        return realized_today + unrealized - baseline['unrealized']

    def _raw_pnl(self, p):
        return (p.get('markPrice', p['entry']) - p['entry']) * p['quantity'] * (1 if p['side'] == 'long' else -1)

    def _daily_loss_exceeded(self):
        latch = self.ledger.get('lossLatch:' + self.mode)
        if latch and latch['day'] == now_ms() // DAY_MS:
            return True
        equity, daily = self._paper_stats() if self.mode == 'paper' else (self._account_equity, self._account_daily)
        daily_baseline = self.ledger.get('daily:' + self.mode)
        baseline = min(max(daily_baseline['equity'] if daily_baseline else equity - daily, 0), self.config['capital'])
        exceeded = baseline <= 0 or daily <= -baseline * self.config['maxDailyLossPercent'] / 100
        if exceeded:
            self.ledger.set('lossLatch:' + self.mode, dict(day=now_ms() // DAY_MS, time=now_ms()))
        return exceeded

    async def status(self):
        positions = self.ledger.positions(mode=self.mode)
        equity, daily = self._paper_stats() if self.mode == 'paper' else (self._account_equity, self._account_daily)
        public = [{k: p.get(k) for k in ('id', 'symbol', 'side', 'entry', 'markPrice', 'quantity', 'stop', 'target', 'pnl', 'openedAt', 'mode')} |
                  {'protection': p.get('state'), 'fundingEstimate': p.get('funding', 0)} for p in positions]
        measured = self.mode == 'paper' or (self._account_at > 0 and now_ms() - self._account_at <= 60_000 and self._account_mode == self.mode)
        baseline = self.ledger.get('daily:' + self.mode, {})
        latch = self.ledger.get('lossLatch:' + self.mode, {})
        return dict(state=self.state, mode=self.mode, credentialsConfigured=self.credentials_configured,
                    liveAllowed=self.live_allowed, config=self.config.copy(), equity=round(equity, 4) if measured else None,
                    dailyPnl=round(daily, 4) if measured else None, realizedPnl=round(self.ledger.cash_total('paper') if self.mode == 'paper' else sum(p.get('pnl', 0) for p in self.ledger.positions(active=False, mode=self.mode)), 4),
                    unrealizedPnl=round(sum(p.get('pnl', 0) for p in positions), 4),
                    externalPositions=sum(1 for x in self._exchange_positions if D(x['positionAmt']) != 0 and x['symbol'] not in {p['symbol'] for p in positions}) if self.mode != 'paper' else 0,
                    dailyBaselineAt=baseline.get('asOf'), dailyBaselineComplete=baseline.get('midnightObserved', False),
                    dailyLossLatched=latch.get('day') == now_ms() // DAY_MS,
                    positions=public, events=self.ledger.events(), error=self.error,
                    telegram=self.notifications.status())

    def _fail(self, exc):
        self.state = 'error'
        self.error = str(exc) if isinstance(exc, BotError) else 'تعذر التأكد من حالة التداول؛ أوقف الدخول وافحص الخادم والمنصة'
        self._event('error', self.error)

    async def _refresh_account(self, force=False):
        if not self.exchange:
            raise BotError('اتصال المنصة غير جاهز')
        if not force and now_ms() - self._account_at < 15_000:
            return
        permissions = await self.exchange.request('GET', '/fapi/v1/accountConfig')
        if permissions.get('canTrade') is not True:
            raise BotError('الحساب أو المفتاح لا يسمح بالتداول')
        if permissions.get('dualSidePosition') is not False or permissions.get('multiAssetsMargin') is not False:
            raise BotError('تغير وضع الحساب؛ يلزم اتجاه واحد وهامش USDT أحادي الأصل')
        account = await self.exchange.request('GET', '/fapi/v3/account')
        assets = [a for a in account.get('assets', []) if a.get('asset') == 'USDT']
        if len(assets) != 1:
            raise BotError('تعذر التحقق من رصيد USDT')
        self._account_equity = float(D(assets[0]['marginBalance']))
        self._available = float(D(assets[0]['availableBalance']))
        self._exchange_positions = await self.exchange.request('GET', '/fapi/v3/positionRisk')
        self._orders = await self.exchange.request('GET', '/fapi/v1/openOrders')
        self._orders.extend(await self.exchange.request('GET', '/fapi/v1/openAlgoOrders'))
        if now_ms() - self._filters_at > 300_000:
            data = await self.exchange.request('GET', '/fapi/v1/exchangeInfo', signed=False)
            self._filters = {s['symbol']: s for s in data['symbols'] if s.get('status') == 'TRADING'
                             and s.get('quoteAsset') == 'USDT' and s.get('contractType') == 'PERPETUAL'}
            self._filters_at = now_ms()
        # Query all cash PNL since UTC midnight. Include fees/funding, exclude transfers.
        income = await self.exchange.request('GET', '/fapi/v1/income', {'startTime': now_ms() // DAY_MS * DAY_MS, 'limit': 1000})
        if len(income) >= 1000:
            raise BotError('سجل اليوم يتجاوز حد المطابقة؛ أوقف الدخول حتى مراجعة الحساب')
        realized = sum(float(D(x['income'])) for x in income if x.get('asset') == 'USDT' and x.get('incomeType') not in ('TRANSFER', 'INTERNAL_TRANSFER'))
        transfers = sum(float(D(x['income'])) for x in income if x.get('asset') == 'USDT' and x.get('incomeType') in ('TRANSFER', 'INTERNAL_TRANSFER'))
        unrealized = float(D(assets[0].get('unrealizedProfit', 0)))
        self._account_daily = self._daily_view(getattr(self.exchange, 'mode', self.mode), self._account_equity, realized, unrealized, transfers)
        self._account_at = now_ms()
        self._account_mode = getattr(self.exchange, 'mode', self.mode)
        self._reconciled = True

    async def _supervise(self):
        while True:
            try:
                async with self._lock:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(exc)
            await asyncio.sleep(5)

    async def _tick(self):
        if self.mode == 'paper':
            await self._monitor_paper()
        elif self.exchange:
            await self._refresh_account()
        if self._daily_loss_exceeded() and (self.state == 'running' or self.ledger.positions(mode=self.mode)):
            self.state = 'paused'
            self.error = 'قاطع الخسارة اليومية: توقف الدخول وتصفية المراكز المدارة'
            queued = self.ledger.get('liquidation', [])
            existing_ids = {q['id'] for q in queued}
            for p in self.ledger.positions(mode=self.mode):
                if p['id'] not in existing_ids:
                    queued.append(dict(id=p['id'], mode=p['mode'], reason='daily-loss'))
            if not self.ledger.get('liquidation', []):
                self._event('circuit', self.error)
            self.ledger.set('liquidation', queued)
        if self.ledger.get('liquidation', []):
            await self._drain_liquidation()
            return
        if self.mode != 'paper' and self.exchange:
            await self._reconcile()
        if self.state != 'running':
            return
        signals = [s for k, s in self.market.signals.items() if k[0] == 'futures' and k[1] == 'intraday' and isinstance(s, dict)]
        for signal in sorted(signals, key=lambda x: x.get('score', 0), reverse=True):
            if self.state != 'running' or self._capacity() <= 0:
                break
            if self._signal_eligible(signal):
                await self._enter(signal)

    async def _drain_liquidation(self):
        queue = self.ledger.get('liquidation', [])
        for item in list(queue):
            if item['mode'] != self.mode:
                continue
            p = next((p for p in self.ledger.positions(mode=self.mode) if p['id'] == item['id']), None)
            try:
                if p:
                    await self._close(p, item['reason'])
                queue.remove(item)
                self.ledger.set('liquidation', queue)
            except Exception as exc:
                # Every position gets an attempt; unknown submitted close ids are
                # only queried by _close, never resubmitted as fresh orders.
                self._fail(exc)

    def _capacity(self):
        if self.mode == 'paper':
            count = len(self.ledger.positions(mode='paper'))
        else:
            # Reserve unrelated positions AND resting entry orders, never take over their symbols.
            symbols = {p['symbol'] for p in self._exchange_positions if D(p['positionAmt']) != 0}
            symbols.update(o['symbol'] for o in self._orders if str(o.get('reduceOnly', False)).lower() != 'true'
                           and str(o.get('closePosition', False)).lower() != 'true')
            symbols.update(p['symbol'] for p in self.ledger.positions(mode=self.mode))
            count = len(symbols)
        return self.config['maxPositions'] - count

    def _signal_eligible(self, s):
        try:
            now = now_ms()
            if s.get('market') != 'futures' or s.get('side') not in ('long', 'short') or not s.get('symbol'):
                return False
            if s.get('horizon') != 'intraday' or s.get('score', 0) < self.config['minScore'] or s.get('riskReward', 0) < self.config['minRiskReward']:
                return False
            if not self._signal_fresh(s):
                return False
            if self.ledger.candle_used(self.mode, s['symbol'], int(s['closedCandleTime'])):
                return False
            if any(p['symbol'] == s['symbol'] for p in self.ledger.positions(mode=self.mode)):
                return False
            if self.mode != 'paper' and (not self._reconciled or now - self._account_at > 20_000 or s['symbol'] not in self._filters):
                return False
            if self.mode != 'paper' and (any(x['symbol'] == s['symbol'] and D(x['positionAmt']) != 0 for x in self._exchange_positions) or any(x['symbol'] == s['symbol'] for x in self._orders)):
                return False
            entry, stop, target = D(s['entry']), D(s['stop']), D(s['targets'][0])
            return entry > 0 and (stop < entry < target if s['side'] == 'long' else target < entry < stop)
        except (KeyError, ValueError, TypeError, IndexError, BotError):
            return False

    def _signal_fresh(self, s):
        now = now_ms()
        return (0 <= now - int(s['asOf']) <= 90_000 and int(s['validUntil']) > now
                and 0 <= now - int(s['closedCandleTime']) <= 240_000)

    async def _check_liquidity(self, symbol, side, mark, quantity, stop):
        book = await self.exchange.request('GET', '/fapi/v1/ticker/bookTicker', {'symbol': symbol}, signed=False)
        bid, ask = D(book.get('bidPrice', 0)), D(book.get('askPrice', 0))
        book_time = int(book.get('time', 0))
        if not 0 <= now_ms() - book_time <= 10_000 or bid <= 0 or ask < bid:
            raise BotError('دفتر الأوامر غير صالح أو قديم؛ الدخول محظور')
        spread = (ask - bid) / mark
        if spread > min(D('0.0015'), abs(mark - stop) / mark * D('0.1')):
            raise BotError('الفارق السعري كبير بالنسبة للمخاطرة')
        immediate_qty = D(book.get('askQty' if side == 'long' else 'bidQty', 0))
        fill_price = ask if side == 'long' else bid
        if quantity * D('1.25') > immediate_qty or abs(fill_price - mark) / mark > SLIPPAGE:
            raise BotError('السيولة الفورية لا تكفي لتعبئة محافظة على الانزلاق')

    def _metadata(self, symbol):
        if self.mode != 'paper':
            raw = self._filters.get(symbol)
            if not raw:
                raise BotError('العملة غير متاحة على منصة التنفيذ')
            fs = {f['filterType']: f for f in raw['filters']}
            lot, market = fs['LOT_SIZE'], fs.get('MARKET_LOT_SIZE', fs['LOT_SIZE'])
            # Both applicable quantity filters are respected.
            return dict(stepSize=max(D(lot['stepSize']), D(market['stepSize'])),
                        minQty=max(D(lot['minQty']), D(market['minQty'])),
                        maxQty=min(D(lot['maxQty']), D(market['maxQty'])),
                        tickSize=D(fs['PRICE_FILTER']['tickSize']),
                        minNotional=D(fs.get('MIN_NOTIONAL', {}).get('notional', 5)))
        item = next((m for m in self.market.symbols.get('futures', []) if m['symbol'] == symbol), None)
        if not item:
            raise BotError('فلاتر العملة غير متاحة؛ لن يتم تخمينها')
        return {k: D(item[k]) for k in ('stepSize', 'minQty', 'tickSize', 'minNotional')} | {'maxQty': D(item.get('maxQty', '1e20'))}

    def _size(self, s, price):
        meta = self._metadata(s['symbol'])
        entry = D(price)
        stop = round_step(D(s['stop']), meta['tickSize'], s['side'] == 'short')
        target = round_step(D(s['targets'][0]), meta['tickSize'], s['side'] == 'short')
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0 or not (stop < entry < target if s['side'] == 'long' else target < entry < stop):
            raise BotError('تحرك السعر خارج خطة التداول')
        unit_cost = entry * (TAKER_FEE * 2 + SLIPPAGE * 2)
        if (reward - unit_cost) / (risk + unit_cost) < D(self.config['minRiskReward']):
            raise BotError('العائد بعد الرسوم والانزلاق أقل من الحد المطلوب')
        equity = D(self._paper_stats()[0] if self.mode == 'paper' else min(self._account_equity, self.config['capital']))
        if self.mode == 'paper':
            margin = sum(p['entry'] * p['quantity'] / self.config['leverage'] for p in self.ledger.positions(mode='paper'))
            available = max(equity - D(margin), D(0))
        else:
            available = min(D(self._available), equity)
        quantity = min(equity * D(self.config['riskPercent']) / 100 / (risk + unit_cost),
                       available * D('0.90') * D(self.config['leverage']) / entry, meta['maxQty'])
        quantity = round_step(quantity, meta['stepSize'])
        if quantity <= 0 or quantity < meta['minQty'] or quantity * entry < meta['minNotional']:
            raise BotError('الرصيد أو ميزانية المخاطر لا تكفي لحدود المنصة')
        return quantity, stop, target

    async def _mark(self, symbol):
        if self.mode != 'paper':
            result = await self.exchange.request('GET', '/fapi/v1/premiumIndex', {'symbol': symbol}, signed=False)
            if now_ms() - int(result['time']) > 15_000 or int(result['time']) > now_ms() + 5000:
                raise BotError('سعر العلامة قديم؛ الدخول محظور')
            return D(result['markPrice']), D(result.get('lastFundingRate', 0))
        snap = await self.market.snapshot('futures', 'intraday')
        if snap.get('connection') != 'live' or now_ms() - int(snap.get('updatedAt', 0)) > 20_000:
            raise BotError('السوق غير مباشر؛ المحاكاة تنتظر بيانات فعلية')
        item = next((x for x in snap['items'] if x['symbol'] == symbol), None)
        if not item or not item.get('price'):
            raise BotError('السعر الحي غير متاح')
        return D(item['price']), D(item.get('fundingRate') or 0)

    async def _market_order(self, symbol, side, quantity, client_id, *, reduce=False):
        params = dict(symbol=symbol, side=side, type='MARKET', quantity=decimal_text(quantity),
                      newClientOrderId=client_id, newOrderRespType='RESULT', positionSide='BOTH')
        if reduce:
            params['reduceOnly'] = 'true'
        self.ledger.intent(client_id, 'close' if reduce else 'entry', params | {'mode': self.mode})
        try:
            order = await self.exchange.request('POST', '/fapi/v1/order', params)
        except Exception:
            # Even a timeout can mean FILLED. The exact id is queried; it is never replaced.
            try:
                order = await self.exchange.request('GET', '/fapi/v1/order', {'symbol': symbol, 'origClientOrderId': client_id})
            except ExchangeError as exc:
                if exc.code == -2013:
                    # A network timeout followed by "not found" remains ambiguous: stop all entries.
                    raise BotError('نتيجة أمر غير مؤكدة؛ لن يعاد إرساله تلقائياً') from None
                raise
        if order.get('status') != 'FILLED' or D(order.get('executedQty', 0)) <= 0 or D(order.get('avgPrice', 0)) <= 0:
            raise BotError('الأمر لم يؤكد تنفيذه الكامل؛ يلزم مطابقة الرصيد')
        self.ledger.complete_intent(client_id)
        return order

    async def _protect(self, p, kind):
        client_id = p['id'] + ('-sl' if kind == 'stop' else '-tp')
        params = dict(symbol=p['symbol'], side='SELL' if p['side'] == 'long' else 'BUY',
                      positionSide='BOTH', algoType='CONDITIONAL',
                      type='STOP_MARKET' if kind == 'stop' else 'TAKE_PROFIT_MARKET',
                      quantity=decimal_text(D(p['quantity'])), triggerPrice=decimal_text(D(p[kind])),
                      reduceOnly='true', workingType='MARK_PRICE', priceProtect='false', clientAlgoId=client_id)
        self.ledger.intent(client_id, kind, params | {'mode': self.mode})
        try:
            await self.exchange.request('POST', '/fapi/v1/algoOrder', params)
        except Exception:
            pass  # Confirmation below is mandatory even after a successful POST response.
        order = await self.exchange.request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': client_id})
        if not self._valid_protection(order, p, kind):
            raise BotError('تعذر تأكيد أمر الحماية على المنصة')
        self.ledger.complete_intent(client_id)
        p[kind + 'Order'] = client_id
        self.ledger.save_position(p)

    def _valid_protection(self, order, p, kind='stop'):
        return (order.get('algoStatus') == 'NEW' and order.get('symbol') == p['symbol']
                and order.get('side') == ('SELL' if p['side'] == 'long' else 'BUY')
                and order.get('orderType') == ('STOP_MARKET' if kind == 'stop' else 'TAKE_PROFIT_MARKET')
                and str(order.get('reduceOnly', False)).lower() == 'true'
                and order.get('workingType') == 'MARK_PRICE'
                and D(order.get('quantity', 0)) >= D(p['quantity'])
                and D(order.get('triggerPrice', 0)) == D(p[kind]))

    async def _enter(self, s):
        symbol = s['symbol']
        try:
            if self.mode != 'paper':
                await self._refresh_account(force=True)
            if self._capacity() <= 0 or self._daily_loss_exceeded() or not self._signal_eligible(s):
                return
            mark, funding = await self._mark(symbol)
            if abs(mark - D(s['entry'])) > abs(D(s['entry']) - D(s['stop'])) * D('0.25'):
                return  # Do not chase a setup after its entry moved.
            price = mark * (1 + SLIPPAGE if s['side'] == 'long' else 1 - SLIPPAGE) if self.mode == 'paper' else mark
            quantity, stop, target = self._size(s, price)
            if self.mode != 'paper':
                await self._check_liquidity(symbol, s['side'], mark, quantity, stop)
        except BotError:
            return  # Ineligible setups are not trading-system failures.
        if not self.ledger.consume_candle(self.mode, symbol, int(s['closedCandleTime'])):
            return
        digest = hashlib.sha256(f"{self.mode}:{symbol}:{s['closedCandleTime']}".encode()).hexdigest()[:20]
        p = dict(id='nb-' + digest, symbol=symbol, side=s['side'], entry=float(price), markPrice=float(mark),
                 quantity=float(quantity), stop=float(stop), target=float(target), pnl=0.0,
                 openedAt=now_ms(), mode=self.mode, state='opening', entryFee=0.0, funding=0.0,
                 lastFundingSlot=now_ms() // 28_800_000, closedCandleTime=s['closedCandleTime'])
        self.ledger.save_position(p)  # Save before any exchange request, so crashes remain reconcilable.
        if self.mode == 'paper':
            p.update(state='protected', entryFee=float(price * quantity * TAKER_FEE))
            self.ledger.cashflow(p['id'] + '-entry-fee', 'paper', 'commission', -p['entryFee'])
            p['pnl'] = self._raw_pnl(p) - p['entryFee']
            self.ledger.save_position(p)
            self._event('entry', f"محاكاة: {symbol} {'شراء' if p['side'] == 'long' else 'بيع'}")
            return
        try:
            # Refuse external positions on this symbol, even if they appeared since signal scan.
            await self._refresh_account(force=True)
            if any(x['symbol'] == symbol and D(x['positionAmt']) != 0 for x in self._exchange_positions):
                raise BotError('ظهر مركز خارجي؛ الدخول محظور')
            if any(x['symbol'] == symbol for x in self._orders) or self._capacity() < 0 or self._daily_loss_exceeded():
                raise BotError('تغيرت أوامر الحساب أو سقف المخاطرة؛ الدخول محظور')
            try:
                await self.exchange.request('POST', '/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'ISOLATED'})
            except ExchangeError as exc:
                if exc.code != -4046:  # "No need to change margin type" is benign.
                    raise
            result = await self.exchange.request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': self.config['leverage']})
            if int(result.get('leverage', 0)) != self.config['leverage'] or quantity * price > D(result.get('maxNotionalValue', 0)):
                raise BotError('رافعة أو سقف المنصة لا يطابق خطة المخاطر')
            configs = await self.exchange.request('GET', '/fapi/v1/symbolConfig', {'symbol': symbol})
            if not configs or configs[0].get('marginType') != 'ISOLATED' or int(configs[0].get('leverage', 0)) != self.config['leverage']:
                raise BotError('تعذر تأكيد الهامش المعزول والرافعة')
            # Account / margin setup can take long enough for the original setup
            # to expire. Reprice and revalidate immediately before the order.
            fresh_mark, _ = await self._mark(symbol)
            if abs(fresh_mark - D(s['entry'])) > abs(D(s['entry']) - D(s['stop'])) * D('0.25'):
                raise BotError('ابتعد السعر عن الدخول قبل إرسال الأمر')
            adverse_fill = fresh_mark * (1 + SLIPPAGE if s['side'] == 'long' else 1 - SLIPPAGE)
            resized, stop, target = self._size(s, adverse_fill)
            quantity = min(quantity, resized)
            await self._check_liquidity(symbol, s['side'], fresh_mark, quantity, stop)
            if not self._signal_fresh(s):
                raise BotError('انتهت صلاحية التوصية قبل إرسال الأمر')
            p.update(entry=float(adverse_fill), quantity=float(quantity), stop=float(stop), target=float(target))
            self.ledger.save_position(p)
            order = await self._market_order(symbol, 'BUY' if p['side'] == 'long' else 'SELL', quantity, p['id'] + '-en')
            p.update(entry=float(D(order['avgPrice'])), quantity=float(D(order['executedQty'])), state='unprotected', entryOrderId=order['orderId'])
            self.ledger.save_position(p)
            allowed_quantity, _, _ = self._size(s, D(order['avgPrice']))
            if D(order['executedQty']) > allowed_quantity:
                raise BotError('التنفيذ الفعلي تجاوز ميزانية المخاطرة؛ إغلاق طارئ')
            await self._protect(p, 'stop')
            await self._protect(p, 'target')
            p['state'] = 'protected'
            self.ledger.save_position(p)
            self._event('entry', f"{symbol}: {'شراء' if p['side'] == 'long' else 'بيع'} | دخول {p['entry']:.8g} | وقف {p['stop']:.8g} | هدف {p['target']:.8g} — تأكدت الحماية على المنصة")
        except Exception as exc:
            self._fail(exc)
            # A failed protection is an emergency: close reduce-only and CONFIRM flat.
            try:
                entry_intent = self.ledger.get_intent(p['id'] + '-en')
                if not entry_intent:
                    # Validation failed BEFORE an entry was sent. Never close another user's position.
                    p.update(state='closed', closedAt=now_ms(), pnl=0.0, closeReason='entry-rejected')
                    self.ledger.save_position(p)
                    return
                if not p.get('entryOrderId'):
                    result = await self.exchange.request('GET', '/fapi/v1/order', {'symbol': symbol, 'origClientOrderId': p['id'] + '-en'})
                    if D(result.get('executedQty', 0)) <= 0 or D(result.get('avgPrice', 0)) <= 0:
                        raise BotError('تعذر تأكيد ملكية التنفيذ؛ لن يغلق البوت مركزاً خارجياً')
                    p.update(entryOrderId=result['orderId'], quantity=float(D(result['executedQty'])), entry=float(D(result['avgPrice'])))
                    self.ledger.save_position(p)
                await self._refresh_account(force=True)
                current = next((x for x in self._exchange_positions if x['symbol'] == symbol and D(x['positionAmt']) != 0), None)
                if current:
                    amount = D(current['positionAmt'])
                    if (amount > 0) != (p['side'] == 'long') or abs(amount) > D(p['quantity']) * D('1.001'):
                        raise BotError('المركز تغير خارج البوت؛ راجع Binance فوراً')
                    p.update(state='unprotected', quantity=float(abs(amount)), entry=float(D(current['entryPrice'])))
                    self.ledger.save_position(p)
                    await self._close(p, 'protection-failure')
                elif not self.ledger.pending_intents():
                    p.update(state='closed', closedAt=now_ms(), pnl=0.0, closeReason='entry-rejected')
                    self.ledger.save_position(p)
            except Exception as close_exc:
                p['state'] = 'unprotected'
                self.ledger.save_position(p)
                self._fail(BotError('خطر: لم يتأكد الإغلاق الطارئ؛ افحص المركز فوراً على Binance. ' + (str(close_exc) if isinstance(close_exc, BotError) else '')))

    async def close_position(self, position_id):
        async with self._lock:
            p = next((p for p in self.ledger.positions(mode=self.mode) if p['id'] == position_id), None)
            if not p:
                raise BotError('المركز غير موجود أو مغلق')
            if self.mode != 'paper' and not self.exchange:
                if not self.credentials_configured:
                    raise BotError('مفاتيح المنصة لازمة لتأكيد الإغلاق')
                await self._connect(self.mode)
            try:
                await self._close(p, 'manual')
            except Exception as exc:
                self._fail(exc)
                raise
        return await self.status()

    async def _cancel_protection(self, p):
        orphans = self.ledger.get('orphanProtection', [])
        for kind in ('stop', 'target'):
            client_id = p.get(kind + 'Order') or p['id'] + ('-sl' if kind == 'stop' else '-tp')
            if not any(o['clientId'] == client_id for o in orphans):
                orphans.append(dict(clientId=client_id, symbol=p['symbol'], mode=p['mode']))
        self.ledger.set('orphanProtection', orphans)
        await self._cleanup_orphans(p['mode'])

    async def _cleanup_orphans(self, mode):
        orphans = self.ledger.get('orphanProtection', [])
        for orphan in list(orphans):
            if orphan['mode'] != mode:
                continue
            client_id = orphan['clientId']
            try:
                await self.exchange.request('DELETE', '/fapi/v1/algoOrder', {'clientAlgoId': client_id})
                try:
                    order = await self.exchange.request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': client_id})
                    if order.get('algoStatus') not in ('CANCELED', 'EXPIRED', 'REJECTED', 'FINISHED'):
                        raise BotError('لم يتأكد إلغاء أمر حماية قديم؛ الدخول محظور')
                except ExchangeError as exc:
                    if exc.code not in (-2011, -2013):
                        raise
            except ExchangeError as exc:
                if exc.code not in (-2011, -2013):
                    raise BotError('تعذر إلغاء حماية قديمة؛ الدخول محظور حتى المطابقة') from None
            self.ledger.complete_intent(client_id, 'cancelled')
            orphans.remove(orphan)
            self.ledger.set('orphanProtection', orphans)

    async def _close(self, p, reason, exit_price=None):
        if p['mode'] == 'paper':
            if exit_price is None:
                mark, _ = await self._mark(p['symbol'])
                exit_price = mark
            exit_price = D(exit_price) * (1 - SLIPPAGE if p['side'] == 'long' else 1 + SLIPPAGE)
            gross = (exit_price - D(p['entry'])) * D(p['quantity']) * (1 if p['side'] == 'long' else -1)
            exit_fee = exit_price * D(p['quantity']) * TAKER_FEE
            self.ledger.cashflow(p['id'] + '-close', 'paper', 'realized', float(gross - exit_fee))
            p.update(state='closed', exit=float(exit_price), exitFee=float(exit_fee), closedAt=now_ms(),
                     pnl=float(gross - exit_fee) - p['entryFee'] - p.get('funding', 0), closeReason=reason)
            self.ledger.save_position(p)
            self._event('close', f"محاكاة: إغلاق {p['symbol']} — {p['pnl']:+.2f} USDT")
            return
        if not p.get('entryOrderId'):
            raise BotError('ملكية تنفيذ الدخول غير مؤكدة؛ لن يغلق البوت مركزاً خارجياً')
        await self._refresh_account(force=True)
        current = next((x for x in self._exchange_positions if x['symbol'] == p['symbol'] and D(x['positionAmt']) != 0), None)
        if current:
            amount = D(current['positionAmt'])
            if (amount > 0) != (p['side'] == 'long') or abs(amount) > D(p['quantity']) * D('1.001'):
                raise BotError('المركز على المنصة تغير خارج البوت؛ الإغلاق التلقائي محظور')
            close_id = p.get('closeClientId') or p['id'] + '-ex'
            if p.get('closeClientId'):
                # Never reuse an accepted id as a new order if a previous attempt remains uncertain.
                previous = await self.exchange.request('GET', '/fapi/v1/order', {'symbol': p['symbol'], 'origClientOrderId': close_id})
                if previous.get('status') == 'FILLED':
                    raise BotError('طلب الإغلاق السابق نفذ لكن المركز ما زال موجوداً؛ يلزم مراجعة')
                raise BotError('طلب إغلاق سابق غير مؤكد؛ لن يعاد تلقائياً')
            p.update(state='closing', closeClientId=close_id, closeReason=reason)
            self.ledger.save_position(p)
            order = await self._market_order(p['symbol'], 'SELL' if amount > 0 else 'BUY', abs(amount), close_id, reduce=True)
            p['exitOrderId'] = order['orderId']
            p['exit'] = float(D(order['avgPrice']))
            self.ledger.save_position(p)
            await self._refresh_account(force=True)
            if any(x['symbol'] == p['symbol'] and D(x['positionAmt']) != 0 for x in self._exchange_positions):
                raise BotError('لم يتأكد خلو المركز على المنصة بعد الإغلاق')
        await self._finalize_exchange_close(p, reason)

    async def _finalize_exchange_close(self, p, reason):
        trades = await self.exchange.request('GET', '/fapi/v1/userTrades', {'symbol': p['symbol'], 'startTime': p['openedAt'], 'limit': 1000})
        if len(trades) >= 1000:
            raise BotError('سجل التنفيذ كبير؛ تعذر تأكيد أرباح المركز')
        ids = {p.get('entryOrderId'), p.get('exitOrderId')}
        for kind in ('stop', 'target'):
            try:
                algo = await self.exchange.request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': p.get(kind + 'Order') or p['id'] + ('-sl' if kind == 'stop' else '-tp')})
                if algo.get('actualOrderId'):
                    ids.add(int(algo['actualOrderId']))
            except ExchangeError as exc:
                if exc.code not in (-2011, -2013):
                    raise
        relevant = [t for t in trades if t.get('orderId') in ids]
        if p.get('entryOrderId') and not any(t.get('orderId') == p['entryOrderId'] for t in relevant):
            raise BotError('المركز خالٍ لكن سجل تنفيذ الدخول لم يظهر؛ تنتظر الخدمة تأكيد الربح والرسوم')
        if p.get('exitOrderId') and not any(t.get('orderId') == p['exitOrderId'] for t in relevant):
            raise BotError('المركز خالٍ لكن سجل تنفيذ الإغلاق لم يظهر؛ تنتظر الخدمة تأكيد الربح والرسوم')
        bot_exit_ids = ids - {None, p.get('entryOrderId')}
        if p.get('entryOrderId') and not any(t.get('orderId') in bot_exit_ids for t in relevant):
            # An external manual / liquidation close is only attributable when
            # its exact opposite-side filled quantity matches the owned entry.
            external = [t for t in trades if t.get('side') == ('SELL' if p['side'] == 'long' else 'BUY')
                        and int(t.get('time', 0)) >= p['openedAt'] and t.get('orderId') != p['entryOrderId']]
            if sum((D(t.get('qty', 0)) for t in external), D(0)) != D(p['quantity']):
                raise BotError('المركز أغلق خارج البوت؛ لم تتأكد نسبة التنفيذ والرسوم لهذا المركز')
            relevant.extend(t for t in external if t not in relevant)
            p['externallyClosed'] = True
        if any(D(t.get('commission', 0)) != 0 and t.get('commissionAsset') != 'USDT' for t in relevant):
            raise BotError('عمولة بعملة أخرى تحتاج مراجعة؛ لن يخمّن البوت صافي الربح')
        net = sum(float(D(t.get('realizedPnl', 0)) - D(t.get('commission', 0))) for t in relevant)
        funding_rows = await self.exchange.request('GET', '/fapi/v1/income', {'symbol': p['symbol'], 'incomeType': 'FUNDING_FEE', 'startTime': p['openedAt'], 'limit': 1000})
        net += sum(float(D(t['income'])) for t in funding_rows)
        p.update(state='closed', closedAt=now_ms(), pnl=net, closeReason=reason, feesConfirmed=True)
        self.ledger.save_position(p)
        await self._cancel_protection(p)
        self._event('close', f"{p['symbol']}: تأكد خلو المركز — {net:+.2f} USDT")

    async def _monitor_paper(self):
        for p in self.ledger.positions(mode='paper'):
            try:
                mark, funding = await self._mark(p['symbol'])
            except BotError:
                continue
            p['markPrice'] = float(mark)
            slot = now_ms() // 28_800_000
            if slot > p.get('lastFundingSlot', slot):
                charge = float(mark * D(p['quantity']) * funding) * (1 if p['side'] == 'long' else -1)
                # Missed funding boundaries are estimated from the current rate and labeled in status.
                charge *= slot - p['lastFundingSlot']
                self.ledger.cashflow(p['id'] + f'-funding-{slot}', 'paper', 'funding-estimate', -charge)
                p['funding'] = p.get('funding', 0) + charge
                p['lastFundingSlot'] = slot
            p['pnl'] = self._raw_pnl(p) - p.get('entryFee', 0) - p.get('funding', 0)
            self.ledger.save_position(p)
            stop_hit = mark <= D(p['stop']) if p['side'] == 'long' else mark >= D(p['stop'])
            target_hit = mark >= D(p['target']) if p['side'] == 'long' else mark <= D(p['target'])
            if stop_hit:
                # Gaps get worse current price, rather than pretending a guaranteed stop fill.
                await self._close(p, 'stop', exit_price=mark)
            elif target_hit:
                await self._close(p, 'target', exit_price=D(p['target']))

    async def _reconcile(self, mode=None):
        mode = mode or self.mode
        if not self.exchange:
            raise BotError('اتصال المنصة مطلوب للمطابقة')
        await self._refresh_account(force=True)
        # Resolve only by exact durable id. Unknown pending entries block any new execution.
        for intent in self.ledger.pending_intents():
            if intent['payload'].get('mode') != mode:
                continue
            try:
                if intent['kind'] in ('entry', 'close'):
                    order = await self.exchange.request('GET', '/fapi/v1/order', {'symbol': intent['payload']['symbol'], 'origClientOrderId': intent['client_id']})
                    if order.get('status') in ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'):
                        self.ledger.complete_intent(intent['client_id'], order['status'].lower())
                        pid = intent['client_id'].rsplit('-', 1)[0]
                        p = next((p for p in self.ledger.positions(mode=mode) if p['id'] == pid), None)
                        if p and order.get('status') == 'FILLED':
                            key = 'entryOrderId' if intent['kind'] == 'entry' else 'exitOrderId'
                            p[key] = order['orderId']
                            if intent['kind'] == 'entry':
                                p.update(entry=float(D(order['avgPrice'])), quantity=float(D(order['executedQty'])), state='unprotected')
                            self.ledger.save_position(p)
                else:
                    order = await self.exchange.request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': intent['client_id']})
                    if order.get('algoStatus'):
                        self.ledger.complete_intent(intent['client_id'], str(order['algoStatus']).lower())
            except ExchangeError as exc:
                if exc.code not in (-2011, -2013):
                    raise
                # Not found does not prove a timed-out entry was never accepted; keep it unresolved.
        check_protection = now_ms() - self._last_protection_check > 15_000
        for p in self.ledger.positions(mode=mode):
            if not p.get('entryOrderId'):
                intent = self.ledger.get_intent(p['id'] + '-en')
                if not intent:
                    p.update(state='closed', closedAt=now_ms(), pnl=0.0, closeReason='entry-not-sent')
                    self.ledger.save_position(p)
                    continue
                try:
                    order = await self.exchange.request('GET', '/fapi/v1/order', {'symbol': p['symbol'], 'origClientOrderId': p['id'] + '-en'})
                    if D(order.get('executedQty', 0)) == 0 and order.get('status') in ('CANCELED', 'EXPIRED', 'REJECTED'):
                        p.update(state='closed', closedAt=now_ms(), pnl=0.0, closeReason='entry-rejected')
                        self.ledger.save_position(p)
                        continue
                    if D(order.get('executedQty', 0)) <= 0 or D(order.get('avgPrice', 0)) <= 0:
                        raise BotError('تنفيذ الدخول غير مؤكد؛ لن يغلق البوت مركزاً خارجياً')
                    p.update(entryOrderId=order['orderId'], entry=float(D(order['avgPrice'])), quantity=float(D(order['executedQty'])), state='unprotected')
                    self.ledger.save_position(p)
                except (ExchangeError, BotError):
                    self._fail(BotError('لم تتأكد ملكية أمر سابق؛ الدخول محظور ويلزم مراجعة Binance'))
                    continue
            current = next((x for x in self._exchange_positions if x['symbol'] == p['symbol'] and D(x['positionAmt']) != 0), None)
            if not current:
                if p['state'] == 'opening' and any(i['client_id'] == p['id'] + '-en' for i in self.ledger.pending_intents()):
                    continue
                await self._finalize_exchange_close(p, 'exchange-reconciled')
                continue
            amount = D(current['positionAmt'])
            if (amount > 0) != (p['side'] == 'long') or abs(amount) > D(p['quantity']) * D('1.001'):
                raise BotError(f"{p['symbol']}: مركز تغير خارج البوت؛ الدخول محظور")
            p.update(quantity=float(abs(amount)), markPrice=float(D(current['markPrice'])), pnl=float(D(current.get('unRealizedProfit', 0))))
            self.ledger.save_position(p)
            if check_protection:
                try:
                    order = await self.exchange.request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': p.get('stopOrder') or p['id'] + '-sl'})
                    target_order = await self.exchange.request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': p.get('targetOrder') or p['id'] + '-tp'})
                    configs = await self.exchange.request('GET', '/fapi/v1/symbolConfig', {'symbol': p['symbol']})
                    if not self._valid_protection(order, p) or not self._valid_protection(target_order, p, 'target') or not configs or configs[0].get('marginType') != 'ISOLATED' or int(configs[0].get('leverage', 0)) != self.config['leverage']:
                        raise BotError('فشل تحقق حماية المركز أو الهامش')
                    p['state'] = 'protected'
                    self.ledger.save_position(p)
                except Exception as exc:
                    self._fail(exc)
                    await self._close(p, 'protection-lost')
        if check_protection:
            self._last_protection_check = now_ms()
        await self._cleanup_orphans(mode)
