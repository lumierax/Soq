"""Transport fixtures test Binance semantics without making network requests."""

import asyncio

import httpx
import pytest

from app.analysis import INTERVAL_MS
from app.market import BinanceHTTPError, CandleCache, MarketService, MarketUnavailable, WeightedRateLimiter, _closed_end_time


NOW = 1_760_000_000_000


def exchange_symbol(symbol="BTCUSDT", market="spot", **changes):
    return {
        "symbol": symbol, "status": "TRADING", "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT", "isSpotTradingAllowed": True,
        "contractType": "PERPETUAL", "marginAsset": "USDT", "pricePrecision": 8,
        "quantityPrecision": 8,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"},
            {"filterType": "MIN_NOTIONAL", "minNotional" if market == "spot" else "notional": "5"},
        ], **changes,
    }


def ticker(symbol="BTCUSDT"):
    return {"symbol": symbol, "lastPrice": "110", "priceChangePercent": "3.4", "quoteVolume": "100000000", "highPrice": "120", "lowPrice": "100"}


def klines(interval="4h", count=180):
    duration = INTERVAL_MS[interval]
    boundary = NOW // duration * duration
    return [[boundary - duration * (count - index), "100", "102", "99", "101", "1000", boundary - duration * (count - index - 1) - 1] for index in range(count)]


@pytest.mark.asyncio
async def test_dynamic_universe_all_usdt_filters_and_delist_purge():
    universes = {"spot": [exchange_symbol(), exchange_symbol("ALTUSDT"), exchange_symbol("OLDUSDT", status="BREAK"), exchange_symbol("ETHBTC", quoteAsset="BTC")],
                 "futures": [exchange_symbol(market="futures"), exchange_symbol("ALTUSDT", market="futures"), exchange_symbol("DATEDUSDT", market="futures", contractType="CURRENT_QUARTER")]}

    def handler(request):
        market = "futures" if request.url.host == "fapi.binance.com" else "spot"
        return httpx.Response(200, json={"symbols": universes[market]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        await service.refresh_universe()
        assert [item["symbol"] for item in service.symbols["spot"]] == ["BTCUSDT", "ALTUSDT"]
        assert len(service.symbols["futures"]) == 2
        metadata = service.symbols["futures"][0]
        assert metadata["pricePrecision"] == 2
        assert metadata["quantityPrecision"] == 3
        assert metadata["minNotional"] == 5
        service.signals[("spot", "swing", "ALTUSDT")] = {"validUntil": NOW + 10_000, "score": 90, "side": "long"}
        service._ticker["spot"]["ALTUSDT"] = {"price": 1}
        service._cache[("spot", "ALTUSDT", "4h")] = CandleCache([], 180, NOW + 10_000, NOW + 10_000)
        universes["spot"] = [exchange_symbol(), exchange_symbol("NEWUSDT")]
        await service.refresh_universe()
        assert [item["symbol"] for item in service.symbols["spot"]] == ["BTCUSDT", "NEWUSDT"]
        assert ("spot", "swing", "ALTUSDT") not in service.signals
        assert "ALTUSDT" not in service._ticker["spot"]
        assert ("spot", "ALTUSDT", "4h") not in service._cache
        with pytest.raises(MarketUnavailable):
            await service.candles("spot", "ALTUSDT", "4h")


@pytest.mark.asyncio
async def test_all_symbols_remain_visible_including_unanalyzed_and_stale():
    moment = [NOW]
    service = MarketService(clock=lambda: moment[0] / 1000)
    service.symbols["spot"] = [exchange_symbol(f"COIN{index}USDT") for index in range(317)]
    service._ticker["spot"] = {item["symbol"]: {"price": 110, "change24h": 3, "quoteVolume": 10_000_000, "high24h": 120, "low24h": 100} for item in service.symbols["spot"]}
    service._ticker_time["spot"] = NOW
    service.signals[("spot", "swing", "COIN0USDT")] = {"side": "long", "score": 95, "validUntil": NOW + 30_000}
    snapshot = await service.snapshot("spot", "swing")
    assert len(snapshot["items"]) == snapshot["totalSymbols"] == 317
    assert snapshot["analyzedSymbols"] == 1
    assert snapshot["items"][0]["symbol"] == "COIN0USDT"
    moment[0] += 31_000
    snapshot = await service.snapshot("spot", "swing")
    assert snapshot["analyzedSymbols"] == 0
    assert all(item["analysis"] is None for item in snapshot["items"])
    moment[0] += 120_000
    snapshot = await service.snapshot("spot", "swing")
    assert snapshot["connection"] == "offline"
    assert len(snapshot["items"]) == 317
    assert all(item["price"] is None for item in snapshot["items"])


@pytest.mark.asyncio
async def test_451_has_no_retries_no_fake_data_and_independent_markets():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "api.binance.com":
            return httpx.Response(451, json={"msg": "restricted location"})
        return httpx.Response(200, json={"symbols": [exchange_symbol(market="futures")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        await service.refresh_universe()
        snapshot = await service.snapshot("spot", "swing")
        assert snapshot["connection"] == "offline"
        assert snapshot["items"] == []
        assert "451" in snapshot["error"]
        assert len(service.symbols["futures"]) == 1
        with pytest.raises(MarketUnavailable):
            await service._request("spot", "/api/v3/ticker/24hr", weight=80)
        assert sum("api.binance.com" in url and "fapi.binance.com" not in url for url in requests) == 1


@pytest.mark.asyncio
async def test_rate_limit_response_enforces_circuit_instead_of_retry_storm():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "90"}, json={"code": -1003})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        with pytest.raises(BinanceHTTPError) as failure:
            await service._request("futures", "/fapi/v1/klines")
        assert failure.value.retry_after == 90
        assert service._blocked_until["futures"] == NOW / 1000 + 90
        with pytest.raises(MarketUnavailable):
            await service._request("futures", "/fapi/v1/klines")
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_kline_requests_coalesce_then_expand_for_history_and_charts():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=klines(request.url.params["interval"], int(request.url.params["limit"])))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        service.symbols["spot"] = [exchange_symbol()]
        batches = await asyncio.gather(*(service._get_candles("spot", "BTCUSDT", "4h", 180, True) for _ in range(6)))
        assert len(calls) == 1
        assert all(len(batch) == 180 for batch in batches)
        chart = await service.candles("spot", "BTCUSDT", "4h", 1000)
        assert len(calls) == 2
        assert len(chart) == 1000
        assert set(chart[0]) == {"time", "open", "high", "low", "close", "volume"}
        await service._get_candles("spot", "BTCUSDT", "4h", 180, True)
        assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_during_request", [False, True])
async def test_forming_chart_cache_is_refetched_at_boundary_before_analysis(cross_during_request):
    duration = INTERVAL_MS["3m"]
    boundary = NOW // duration * duration
    moment = [boundary - 1000]
    calls = []
    forming = [boundary - duration, "100", "102", "99", "101", "10", boundary - 1]
    confirmed = [boundary - duration, "100", "110", "99", "109", "500", boundary - 1]
    prior = [boundary - duration * 2, "100", "101", "98", "100", "100", boundary - duration - 1]

    def handler(request):
        calls.append(request)
        if "endTime" not in request.url.params:
            if cross_during_request:
                moment[0] = boundary + 500
            return httpx.Response(200, json=[prior, forming])
        assert int(request.url.params["endTime"]) == boundary - 1
        return httpx.Response(200, json=[prior, confirmed])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: moment[0] / 1000)
        service.symbols["futures"] = [exchange_symbol(market="futures")]
        rows = await service._get_candles("futures", "BTCUSDT", "3m", 180)
        assert rows[-1]["close"] == 101
        assert service._cache[("futures", "BTCUSDT", "3m")].analysis_expires == boundary
        moment[0] = boundary + 500
        completed = await service._get_candles("futures", "BTCUSDT", "3m", 180, True)
        assert len(calls) == 2
        assert completed[-1]["close"] == 109
        assert completed[-1]["volume"] == 500


def test_weekly_closed_boundary_is_monday_utc():
    monday = 4 * INTERVAL_MS["1d"] + INTERVAL_MS["1w"] * 2900
    assert _closed_end_time("1w", monday + INTERVAL_MS["1d"] * 3) == monday - 1
    assert _closed_end_time("1w", monday) == monday - 1


@pytest.mark.asyncio
async def test_ticker_normalizes_numeric_data_without_cross_market_leakage():
    def handler(request):
        return httpx.Response(200, json=[ticker(), ticker("NOTLISTEDUSDT"), {**ticker("BADUSDT"), "lastPrice": "NaN"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        service.symbols["spot"] = [exchange_symbol(), exchange_symbol("BADUSDT")]
        await service._refresh_tickers("spot")
        snapshot = await service.snapshot("spot", "swing")
        items = {item["symbol"]: item for item in snapshot["items"]}
        assert items["BTCUSDT"]["price"] == 110
        assert items["BTCUSDT"]["quoteVolume"] == 100_000_000
        assert items["BADUSDT"]["price"] is None
        assert service._ticker["futures"] == {}


@pytest.mark.asyncio
async def test_historical_funding_preserves_actual_rate_and_order():
    def handler(request):
        assert request.url.path == "/fapi/v1/fundingRate"
        assert request.url.params["limit"] == "1000"
        return httpx.Response(200, json=[
            {"symbol": "BTCUSDT", "fundingTime": NOW - 1, "fundingRate": "-0.00025000", "markPrice": "110.25"},
            {"symbol": "BTCUSDT", "fundingTime": NOW - 1000, "fundingRate": "0.00010000"},
        ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        service.symbols["futures"] = [exchange_symbol(market="futures")]
        records = await service.historical_funding("BTCUSDT")
        assert [record["fundingTime"] for record in records] == [NOW - 1000, NOW - 1]
        assert records[-1]["fundingRate"] == "-0.00025000"


@pytest.mark.asyncio
async def test_validation_refuses_wrong_horizons_and_intervals_without_request():
    service = MarketService()
    with pytest.raises(ValueError):
        await service.snapshot("futures", "investment")
    with pytest.raises(ValueError):
        await service.candles("spot", "BTCUSDT", "3m")
    with pytest.raises(ValueError):
        await service.candles("futures", "BTCUSDT", "1h", 1001)
    assert service._client is None


@pytest.mark.asyncio
async def test_service_start_stop_is_idempotent_and_cancels_background_tasks():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(451))) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        await service.start()
        await service.start()
        assert len(service._tasks) == 7
        tasks = list(service._tasks)
        await asyncio.sleep(0.02)
        await service.stop()
        await service.stop()
        assert all(task.done() for task in tasks)
        assert not service.status()["running"]
        assert not client.is_closed


@pytest.mark.asyncio
async def test_limiter_reservations_are_atomic_under_concurrency():
    limiter = WeightedRateLimiter(2, window_seconds=0.01)
    await asyncio.gather(*(limiter.acquire(1) for _ in range(5)))
    assert limiter._used <= 2
    assert len(limiter._events) <= 2
    with pytest.raises(ValueError):
        await limiter.acquire(3)


@pytest.mark.asyncio
async def test_scanner_visits_every_symbol_without_ranking_selection(monkeypatch):
    service = MarketService(clock=lambda: NOW / 1000, scan_concurrency=3)
    service.symbols["futures"] = [exchange_symbol(market="futures")] + [exchange_symbol(f"COIN{index}USDT", market="futures") for index in range(127)]
    service._running = True
    visited = []

    async def analyze(market, symbol, horizon):
        visited.append(symbol)
        return {"side": "wait", "score": 0}

    async def stop_after_sweep(seconds):
        if seconds == 30:
            service._running = False

    monkeypatch.setattr(service, "analyze_symbol", analyze)
    monkeypatch.setattr("app.market.asyncio.sleep", stop_after_sweep)
    await service._scan_loop("futures", "intraday")
    assert set(visited) == {item["symbol"] for item in service.symbols["futures"]}
    assert service._scan_counts[("futures", "intraday")] == 128


@pytest.mark.asyncio
async def test_delist_during_candle_request_cannot_publish_old_data():
    service = None

    def handler(request):
        service.symbols["spot"] = []
        return httpx.Response(200, json=klines())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000)
        service.symbols["spot"] = [exchange_symbol()]
        with pytest.raises(MarketUnavailable):
            await service._get_candles("spot", "BTCUSDT", "4h", 180, True)
        assert service._cache == {}


@pytest.mark.asyncio
async def test_server_error_retries_are_bounded_and_transient_recovery_works(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"serverTime": NOW})

    async def no_delay(seconds):
        return None

    monkeypatch.setattr("app.market.asyncio.sleep", no_delay)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(client, clock=lambda: NOW / 1000, retries=1)
        assert await service._request("spot", "/api/v3/time") == {"serverTime": NOW}
        assert len(calls) == 2
