"""Safety / lifecycle tests. All exchange traffic is mocked; none places real orders."""
from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.bot import BinanceFutures, BotError, BotService, D, ExchangeError, round_step
from app.storage import now_ms


class Market:
    def __init__(self):
        self.price = 100.0
        self.connection = 'live'
        self.signals = {}
        self.symbols = {'futures': [dict(symbol='BTCUSDT', stepSize='0.001', minQty='0.001',
                                       tickSize='0.01', minNotional='5')]}

    async def snapshot(self, market, horizon):
        return dict(connection=self.connection, updatedAt=now_ms(),
                    items=[dict(symbol='BTCUSDT', price=self.price, fundingRate=0)])


def signal(**changes):
    return dict(symbol='BTCUSDT', market='futures', horizon='intraday', side='long',
                asOf=now_ms(), validUntil=now_ms()+180_000, closedCandleTime=now_ms()-1000,
                score=90, riskReward=3, entry=100, stop=95, targets=[115, 120, 125], **changes)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DB_PATH', str(tmp_path / 'ledger.sqlite3'))
    monkeypatch.delenv('BINANCE_API_KEY', raising=False)
    monkeypatch.delenv('BINANCE_API_SECRET', raising=False)
    monkeypatch.delenv('ALLOW_LIVE_TRADING', raising=False)
    return BotService(Market())


@pytest.mark.asyncio
async def test_live_requires_server_allow_flag_credentials_and_exact_confirmation(bot):
    with pytest.raises(BotError):
        await bot.arm('live', 'ENABLE LIVE TRADING')
    assert bot.state != 'running'
    with pytest.raises(BotError):
        await bot.arm('testnet')
    assert not (await bot.status())['credentialsConfigured']


@pytest.mark.asyncio
async def test_invalid_config_and_nonfinite_fail_closed(bot):
    for settings in ({'leverage': 20}, {'maxPositions': 1.5}, {'capital': float('nan')}, {'secret': 'x'}, {'minScore': True}):
        with pytest.raises(BotError):
            await bot.configure(settings)
    assert bot.config['leverage'] == 3


@pytest.mark.asyncio
async def test_paper_uses_real_prices_costs_and_durable_no_reentry(bot):
    await bot.arm('paper')
    s = signal()
    await bot._enter(s)
    positions = (await bot.status())['positions']
    assert len(positions) == 1
    p = positions[0]
    assert p['entry'] > 100  # Slippage hurts entry.
    assert p['pnl'] < 0  # Entry commission is charged immediately.
    with pytest.raises(BotError):
        await bot.configure({'riskPercent': 4})
    bot.market.price = 110
    await bot.close_position(p['id'])
    history = await bot.history()
    assert history[0]['exit'] < 110
    assert history[0]['entryFee'] > 0 and history[0]['exitFee'] > 0
    assert history[0]['pnl'] > 0
    assert (await bot.status())['unrealizedPnl'] == 0
    await bot._enter(s)
    assert not bot.ledger.positions()
    restarted = BotService(bot.market)
    assert restarted.state == 'stopped'
    assert restarted.ledger.candle_used('paper', s['symbol'], s['closedCandleTime'])


@pytest.mark.asyncio
async def test_no_fake_paper_entries_during_outage(bot):
    await bot.arm('paper')
    bot.market.connection = 'offline'
    await bot._enter(signal())
    assert bot.ledger.positions() == []


def test_stale_low_score_and_bad_rr_signals_are_rejected(bot):
    for key, value in [('asOf', now_ms()-100_000), ('validUntil', now_ms()-1),
                       ('closedCandleTime', now_ms()-300_000), ('score', 70),
                       ('riskReward', 1.2), ('side', 'wait')]:
        s = signal()
        s[key] = value
        assert not bot._signal_eligible(s)


def test_decimal_size_respects_step_risk_and_net_rr(bot):
    quantity, stop, target = bot._size(signal(), D(100))
    assert quantity % D('0.001') == 0
    assert quantity * (D(5) + D('.30')) <= 10
    s = signal()
    s['targets'][0] = 110
    with pytest.raises(BotError):
        bot._size(s, D(100))  # Gross 2R fails after fees and slippage.
    assert round_step(D('1.23456'), D('.001')) == Decimal('1.234')
    with pytest.raises(BotError):
        D(float('inf'))


@pytest.mark.asyncio
async def test_daily_circuit_includes_open_losses_and_survives_restart(bot):
    await bot.arm('paper')
    p = dict(id='nb-existing', symbol='BTCUSDT', side='long', entry=100, markPrice=80,
             quantity=3, stop=70, target=140, pnl=-60, openedAt=now_ms(), mode='paper',
             state='protected', entryFee=0, funding=0)
    bot.ledger.save_position(p)
    bot.market.price = 80
    await bot._tick()
    assert bot.state == 'paused'
    assert not bot.ledger.positions()
    assert bot.ledger.positions(active=False)[0]['closeReason'] == 'daily-loss'
    restarted = BotService(bot.market)
    with pytest.raises(BotError):
        await restarted.arm('paper')
    assert restarted.state == 'error'


@pytest.mark.asyncio
async def test_pause_continues_position_monitoring(bot):
    await bot.arm('paper')
    await bot._enter(signal())
    await bot.pause()
    bot.market.price = 94
    await bot._tick()
    assert bot.state == 'paused'
    assert not bot.ledger.positions()
    assert bot.ledger.positions(active=False)[0]['closeReason'] == 'stop'


def test_total_position_cap_reserves_external_and_resting_entries(bot):
    bot.mode = 'testnet'
    bot._exchange_positions = [dict(symbol='ETHUSDT', positionAmt='1')]
    bot._orders = [dict(symbol='SOLUSDT', reduceOnly=False)]
    assert bot._capacity() == 1
    bot._orders.append(dict(symbol='XRPUSDT', reduceOnly=True))
    assert bot._capacity() == 1
    bot._exchange_positions.append(dict(symbol='XRPUSDT', positionAmt='1'))
    assert bot._capacity() == 0


@pytest.mark.asyncio
async def test_signing_percent_encodes_before_signature_and_never_resubmits_unknown_order(bot):
    transport_calls = []
    def handler(request):
        transport_calls.append(request)
        raw = request.url.query.decode()
        encoded, sig = raw.rsplit('&signature=', 1)
        assert sig == hmac.new(b'server-secret', encoded.encode(), hashlib.sha256).hexdigest()
        assert '%E6%B5%8B' in encoded
        assert request.headers['X-MBX-APIKEY'] == 'server-key'
        return httpx.Response(200, json={'ok': True})
    exchange = BinanceFutures('testnet', 'server-key', 'server-secret')
    await exchange.client.aclose()
    exchange.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await exchange.request('POST', '/fapi/v1/order', {'symbol': '测试USDT'})
    await exchange.close()
    assert len(transport_calls) == 1

    class TimeoutExchange:
        def __init__(self): self.calls = []
        async def request(self, method, path, params=None, **kw):
            self.calls.append((method, path, params))
            raise ExchangeError(0 if method == 'POST' else -2013, 'unknown')
    bot.exchange = TimeoutExchange()
    bot.mode = 'testnet'
    with pytest.raises(BotError):
        await bot._market_order('BTCUSDT', 'BUY', D(1), 'nb-unique-en')
    assert len([c for c in bot.exchange.calls if c[0] == 'POST']) == 1
    assert len(bot.ledger.pending_intents()) == 1


class Exchange:
    """Deterministic one-way isolated mocked Binance with optional protective failure."""
    def __init__(self, protect=True, external_after=False):
        self.calls = []
        self.amount = D(0)
        self.price = D(100)
        self.protect = protect
        self.external_after = external_after
        self.orders = {}
        self.algos = {}
        self.next_id = 10

    async def request(self, method, path, params=None, **kwargs):
        params = params or {}
        self.calls.append((method, path, dict(params)))
        if path == '/fapi/v1/accountConfig':
            return dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False)
        if path == '/fapi/v3/account':
            return dict(assets=[dict(asset='USDT', marginBalance='1000', availableBalance='1000', unrealizedProfit='0')])
        if path == '/fapi/v3/positionRisk':
            return [dict(symbol='BTCUSDT', positionAmt=str(self.amount), entryPrice='100', markPrice='100', unRealizedProfit='0')] if self.amount else []
        if path in ('/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders', '/fapi/v1/income'):
            return []
        if path == '/fapi/v1/userTrades':
            return [dict(orderId=order['orderId'], realizedPnl='0', commission='0.05', commissionAsset='USDT') for order in self.orders.values()]
        if path == '/fapi/v1/premiumIndex':
            return dict(markPrice='100', time=now_ms(), lastFundingRate='0')
        if path == '/fapi/v1/ticker/bookTicker':
            return dict(bidPrice='99.99', askPrice='100.01', bidQty='1000', askQty='1000', time=now_ms())
        if path == '/fapi/v1/marginType':
            return dict(code=200)
        if path == '/fapi/v1/leverage':
            return dict(leverage=3, maxNotionalValue='1000000')
        if path == '/fapi/v1/symbolConfig':
            return [dict(symbol='BTCUSDT', marginType='ISOLATED', leverage=3)]
        if path == '/fapi/v1/order' and method == 'POST':
            quantity = D(params['quantity'])
            self.amount += quantity * (1 if params['side'] == 'BUY' else -1)
            self.next_id += 1
            result = dict(status='FILLED', orderId=self.next_id, executedQty=str(quantity), avgPrice='100')
            self.orders[params['newClientOrderId']] = result
            return result
        if path == '/fapi/v1/order' and method == 'GET':
            return self.orders[params['origClientOrderId']]
        if path == '/fapi/v1/algoOrder':
            cid = params['clientAlgoId']
            if method == 'POST':
                if not self.protect:
                    raise ExchangeError(-1111, 'protection rejected')
                self.algos[cid] = dict(algoStatus='NEW', symbol=params['symbol'], side=params['side'],
                                      orderType=params['type'], quantity=params['quantity'],
                                      triggerPrice=params['triggerPrice'], reduceOnly=True, workingType='MARK_PRICE')
                return self.algos[cid]
            if method == 'DELETE':
                self.algos.pop(cid, None)
                return dict(code=200)
            if cid not in self.algos:
                raise ExchangeError(-2013, 'not found')
            return self.algos[cid]
        raise AssertionError((method, path, params))


def configure_exchange(bot, exchange):
    bot.mode = 'testnet'
    bot.state = 'running'
    bot.exchange = exchange
    bot._reconciled = True
    bot._account_at = now_ms()
    bot._filters_at = now_ms()
    bot._account_equity = bot._available = 1000
    bot._filters = {'BTCUSDT': dict(filters=[
        dict(filterType='LOT_SIZE', stepSize='0.001', minQty='0.001', maxQty='1000'),
        dict(filterType='MARKET_LOT_SIZE', stepSize='0.001', minQty='0.001', maxQty='500'),
        dict(filterType='PRICE_FILTER', tickSize='0.01'),
        dict(filterType='MIN_NOTIONAL', notional='5'),
    ])}


@pytest.mark.asyncio
async def test_exchange_stop_failure_emergency_reduce_only_close_and_disarms(bot):
    exchange = Exchange(protect=False)
    configure_exchange(bot, exchange)
    await bot._enter(signal())
    assert bot.state == 'error'
    assert exchange.amount == 0
    assert not bot.ledger.positions()
    orders = [params for method, path, params in exchange.calls if method == 'POST' and path == '/fapi/v1/order']
    assert len(orders) == 2
    assert orders[0]['side'] == 'BUY'
    assert orders[1]['side'] == 'SELL' and orders[1]['reduceOnly'] == 'true'
    assert bot.ledger.positions(active=False)[0]['closeReason'] == 'protection-failure'
    assert not bot.ledger.pending_intents()


@pytest.mark.asyncio
async def test_exchange_entry_is_managed_only_after_both_algo_orders_confirmed(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    await bot._enter(signal())
    p = bot.ledger.positions()[0]
    assert p['state'] == 'protected'
    protective = [params for method, path, params in exchange.calls if method == 'POST' and path == '/fapi/v1/algoOrder']
    assert {p['type'] for p in protective} == {'STOP_MARKET', 'TAKE_PROFIT_MARKET'}
    assert all(p['reduceOnly'] == 'true' and 'triggerPrice' in p and 'stopPrice' not in p and 'closePosition' not in p for p in protective)
    assert len([c for c in exchange.calls if c[0] == 'GET' and c[1] == '/fapi/v1/algoOrder']) >= 2


@pytest.mark.asyncio
async def test_changed_external_position_is_never_closed_before_own_entry(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    refreshes = 0
    original = bot._refresh_account
    async def refresh(force=False):
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            exchange.amount = D(5)  # Another user opened a position after the first check.
        await original(force=True)
    bot._refresh_account = refresh
    await bot._enter(signal())
    assert exchange.amount == 5
    assert not [c for c in exchange.calls if c[0] == 'POST' and c[1] == '/fapi/v1/order']
    assert bot.state == 'error'


@pytest.mark.asyncio
async def test_restart_reconciles_and_closes_unprotected_without_auto_arming(bot, monkeypatch):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    await bot._enter(signal())
    p = bot.ledger.positions()[0]
    exchange.algos.clear()  # Stop disappeared while service was offline.
    bot.state = 'stopped'
    await bot._reconcile()
    assert bot.state == 'error'
    assert exchange.amount == 0
    assert not bot.ledger.positions()


@pytest.mark.asyncio
async def test_restart_save_before_intent_gap_never_takes_over_external_position(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    p = dict(id='nb-gap', symbol='BTCUSDT', side='long', entry=100, markPrice=100,
             quantity=1, stop=95, target=115, pnl=0, openedAt=now_ms(), mode='testnet', state='opening')
    bot.ledger.save_position(p)  # Crash after position save, before any entry intent / request.
    exchange.amount = D(1)  # A human subsequently opened the same symbol.
    bot.state = 'stopped'
    await bot._reconcile()
    assert exchange.amount == 1
    assert not bot.ledger.positions()
    assert bot.ledger.positions(active=False)[0]['closeReason'] == 'entry-not-sent'
    assert not [c for c in exchange.calls if c[0] == 'POST' and c[1] == '/fapi/v1/order']


@pytest.mark.asyncio
async def test_restart_uncertain_ownership_disarms_without_touching_external_position(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    p = dict(id='nb-uncertain', symbol='BTCUSDT', side='long', entry=100, markPrice=100,
             quantity=1, stop=95, target=115, pnl=0, openedAt=now_ms(), mode='testnet', state='opening')
    bot.ledger.save_position(p)
    bot.ledger.intent(p['id']+'-en', 'entry', dict(symbol='BTCUSDT', mode='testnet'))
    exchange.amount = D(1)
    original = exchange.request
    async def missing(method, path, params=None, **kwargs):
        if path == '/fapi/v1/order' and method == 'GET':
            raise ExchangeError(-2013, 'not found')
        return await original(method, path, params, **kwargs)
    exchange.request = missing
    bot.state = 'stopped'
    await bot._reconcile()
    assert bot.state == 'error'
    assert exchange.amount == 1
    assert len(bot.ledger.pending_intents()) == 1
    assert not [c for c in exchange.calls if c[0] == 'POST' and c[1] == '/fapi/v1/order']


@pytest.mark.asyncio
async def test_wide_spread_rejects_entry_before_consuming_candle(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    original = exchange.request
    async def wide(method, path, params=None, **kwargs):
        if path == '/fapi/v1/ticker/bookTicker':
            return dict(bidPrice='99', askPrice='101', bidQty='1000', askQty='1000', time=now_ms())
        return await original(method, path, params, **kwargs)
    exchange.request = wide
    s = signal()
    await bot._enter(s)
    assert not bot.ledger.positions()
    assert not bot.ledger.candle_used('testnet', 'BTCUSDT', s['closedCandleTime'])


@pytest.mark.asyncio
async def test_unvalidated_or_stale_exchange_balances_are_unknown(bot):
    bot.mode = 'live'
    result = await bot.status()
    assert result['equity'] is None and result['dailyPnl'] is None
    bot._account_at = now_ms()-70_000
    bot._account_mode = 'live'
    bot._account_equity = 1000
    result = await bot.status()
    assert result['equity'] is None
    bot._account_at = now_ms()
    result = await bot.status()
    assert result['equity'] == 1000


@pytest.mark.asyncio
async def test_daily_circuit_attempts_all_positions_and_keeps_failed_close_for_next_tick(bot):
    await bot.arm('paper')
    for i in range(2):
        bot.ledger.save_position(dict(id=f'nb-loss-{i}', symbol=f'LOSS{i}USDT', side='long', entry=100,
                                      markPrice=50, quantity=2, stop=40, target=120, pnl=-100,
                                      openedAt=now_ms(), mode='paper', state='protected', entryFee=0))
    async def no_monitor(): pass
    bot._monitor_paper = no_monitor
    attempts = []
    fail_once = True
    async def close(p, reason, exit_price=None):
        nonlocal fail_once
        attempts.append(p['id'])
        if p['id'] == 'nb-loss-0' and fail_once:
            fail_once = False
            raise BotError('transient')
        p.update(state='closed', closedAt=now_ms(), closeReason=reason)
        bot.ledger.save_position(p)
    bot._close = close
    await bot._tick()
    assert set(attempts) == {'nb-loss-0', 'nb-loss-1'}
    assert len(bot.ledger.get('liquidation')) == 1
    assert bot.state == 'error'
    await bot._tick()
    assert attempts.count('nb-loss-0') == 2
    assert not bot.ledger.positions()
    assert bot.ledger.get('liquidation') == []


@pytest.mark.asyncio
async def test_signal_expiring_during_account_preparation_never_sends_entry(bot, monkeypatch):
    import app.bot as bot_module
    exchange = Exchange()
    configure_exchange(bot, exchange)
    clock = now_ms()
    monkeypatch.setattr(bot_module, 'now_ms', lambda: clock)
    s = signal()
    s['validUntil'] = clock + 1000
    original = exchange.request
    async def delayed(method, path, params=None, **kwargs):
        nonlocal clock
        result = await original(method, path, params, **kwargs)
        if path == '/fapi/v1/symbolConfig':
            clock += 3000
        return result
    exchange.request = delayed
    await bot._enter(s)
    assert not [c for c in exchange.calls if c[0] == 'POST' and c[1] == '/fapi/v1/order']
    assert not bot.ledger.positions()


@pytest.mark.asyncio
async def test_actual_fill_over_risk_budget_emergency_closes(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    original = exchange.request
    async def poor_fill(method, path, params=None, **kwargs):
        result = await original(method, path, params, **kwargs)
        if method == 'POST' and path == '/fapi/v1/order' and not params.get('reduceOnly'):
            result['avgPrice'] = '105'
        return result
    exchange.request = poor_fill
    await bot._enter(signal())
    assert exchange.amount == 0 and bot.state == 'error'
    assert not bot.ledger.positions()
    assert bot.ledger.positions(active=False)[0]['closeReason'] == 'protection-failure'


@pytest.mark.asyncio
async def test_resting_external_order_appearing_on_second_account_refresh_blocks_entry(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    original = exchange.request
    refreshes = 0
    async def competing(method, path, params=None, **kwargs):
        nonlocal refreshes
        if path == '/fapi/v1/openOrders':
            refreshes += 1
            return [dict(symbol='BTCUSDT', reduceOnly=False)] if refreshes >= 2 else []
        return await original(method, path, params, **kwargs)
    exchange.request = competing
    await bot._enter(signal())
    assert not [c for c in exchange.calls if c[0] == 'POST' and c[1] == '/fapi/v1/order']
    assert exchange.amount == 0


@pytest.mark.asyncio
async def test_missing_target_closes_managed_position(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    await bot._enter(signal())
    p = bot.ledger.positions()[0]
    del exchange.algos[p['targetOrder']]
    await bot._reconcile()
    assert bot.state == 'error'
    assert exchange.amount == 0
    assert not bot.ledger.positions()


def test_daily_pnl_rebases_overnight_unrealized_and_loss_latches(bot, monkeypatch):
    import app.bot as bot_module
    day_ms = 86_400_000
    clock = (now_ms() // day_ms + 1) * day_ms - 5000
    monkeypatch.setattr(bot_module, 'now_ms', lambda: clock)
    assert bot._daily_view('live', 1100, 0, 100) == 0
    clock += 10_000
    assert bot._daily_view('live', 980, 0, -20) == -120
    bot.mode = 'live'
    bot._account_equity = 980
    bot._account_daily = -120
    assert bot._daily_loss_exceeded()
    # Deposits / later gains cannot reset a tripped day.
    bot._account_equity = 2000
    bot._account_daily = 100
    assert bot._daily_loss_exceeded()
    assert bot.ledger.get('daily:live')['midnightObserved'] is True
    clock += day_ms
    bot._daily_view('live', 2000, 0, 0)
    bot._account_daily = 0
    assert not bot._daily_loss_exceeded()


def test_deposit_does_not_expand_daily_loss_reference(bot):
    bot._daily_view('live', 6000, -20, 0, transfers=5000)
    assert bot.ledger.get('daily:live')['equity'] == 1020


@pytest.mark.asyncio
async def test_failed_orphan_order_cancellation_is_durable_and_disarms(bot):
    exchange = Exchange()
    configure_exchange(bot, exchange)
    await bot._enter(signal())
    p = bot.ledger.positions()[0]
    original = exchange.request
    async def cannot_cancel(method, path, params=None, **kwargs):
        if method == 'DELETE' and path == '/fapi/v1/algoOrder':
            raise ExchangeError(503, 'unavailable')
        return await original(method, path, params, **kwargs)
    exchange.request = cannot_cancel
    with pytest.raises(BotError):
        await bot.close_position(p['id'])
    assert exchange.amount == 0
    assert bot.state == 'error'
    assert len(bot.ledger.get('orphanProtection')) == 2
    exchange.request = original
    await bot._reconcile()
    assert bot.ledger.get('orphanProtection') == []
