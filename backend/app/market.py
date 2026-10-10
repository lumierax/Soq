"""Public Binance data with bounded requests, dynamic universes and no fallback data."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx

from .analysis import (
    AnalysisUnavailable, INTERVAL_MS, REQUIRED_TIMEFRAMES,
    analyze_candles, regime_from_candles,
)


PUBLIC_ENDPOINTS = {"spot": "https://api.binance.com", "futures": "https://fapi.binance.com"}
ALLOWED_INTERVALS = {"spot": ("4h", "1d", "1w"), "futures": ("3m", "5m", "15m", "30m", "1h")}
HORIZONS = {"spot": ("swing", "investment"), "futures": ("intraday",)}


class MarketUnavailable(RuntimeError):
    """An upstream feed or symbol is unavailable; no fabricated replacement."""


class BinanceHTTPError(MarketUnavailable):
    def __init__(self, status_code: int, message: str, retry_after: float = 0):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class WeightedRateLimiter:
    """Sliding minute window shared by each market, including concurrent workers.

    Default budgets deliberately leave headroom below Binance's published
    limits. Other programs sharing the same IP can still consume its quota.
    """
    def __init__(self, capacity: int, window_seconds: float = 60):
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0
        self._lock = asyncio.Lock()
        self._pause_until = 0.0

    async def acquire(self, weight: int) -> None:
        if weight <= 0 or weight > self.capacity:
            raise ValueError("request weight exceeds configured budget")
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0][0] >= self.window_seconds:
                    self._used -= self._events.popleft()[1]
                if now >= self._pause_until and self._used + weight <= self.capacity:
                    self._events.append((now, weight))
                    self._used += weight
                    return
                until_budget = max(0.0, self.window_seconds - (now - self._events[0][0])) if self._events else 0.0
                delay = max(self._pause_until - now, until_budget if self._used + weight > self.capacity else 0.0)
            await asyncio.sleep(min(max(delay, 0.01), 1.0))

    def observe(self, headers: httpx.Headers) -> None:
        try:
            used = int(headers.get("x-mbx-used-weight-1m", "0"))
            if used >= self.capacity:
                self._pause_until = max(self._pause_until, time.monotonic() + 60)
        except ValueError:
            pass

    def pause(self, seconds: float) -> None:
        self._pause_until = max(self._pause_until, time.monotonic() + max(0.0, seconds))

    def status(self) -> dict[str, int]:
        return {"capacityPerMinute": self.capacity, "reservedWeight": self._used}


@dataclass
class CandleCache:
    rows: list[dict[str, Any]]
    requested_limit: int
    live_expires: int
    analysis_expires: int
    closed_only: bool = False


def _positive_number(value: Any, allow_zero: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError("invalid exchange number")
    return number


def _precision(value: str, fallback: int) -> int:
    try:
        decimal = Decimal(value).normalize()
        return max(0, -decimal.as_tuple().exponent) if decimal > 0 else fallback
    except (InvalidOperation, TypeError):
        return fallback


def _closed_end_time(interval: str, now_ms: int) -> int:
    # Binance weekly candles begin Monday UTC. The Unix epoch began Thursday.
    offset = 4 * INTERVAL_MS["1d"] if interval == "1w" else 0
    duration = INTERVAL_MS[interval]
    return (now_ms - offset) // duration * duration + offset - 1


def symbol_metadata(item: dict[str, Any], market: str) -> dict[str, Any] | None:
    if item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT":
        return None
    if market == "spot" and item.get("isSpotTradingAllowed") is False:
        return None
    if market == "futures" and (item.get("contractType") != "PERPETUAL" or item.get("marginAsset", "USDT") != "USDT"):
        return None
    filters = {value["filterType"]: value for value in item.get("filters", []) if "filterType" in value}
    price_filter = filters.get("PRICE_FILTER", {})
    lot_filter = filters.get("LOT_SIZE", {})
    notional_filter = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
    tick_size = str(price_filter.get("tickSize", "0"))
    step_size = str(lot_filter.get("stepSize", "0"))
    try:
        return {
            "symbol": str(item["symbol"]), "baseAsset": str(item["baseAsset"]), "quoteAsset": "USDT",
            "pricePrecision": _precision(tick_size, int(item.get("pricePrecision", item.get("quotePrecision", 8)))),
            "quantityPrecision": _precision(step_size, int(item.get("quantityPrecision", item.get("baseAssetPrecision", 8)))),
            "tickSize": _positive_number(tick_size, True), "stepSize": _positive_number(step_size, True),
            "minQty": _positive_number(lot_filter.get("minQty", "0"), True),
            "minNotional": _positive_number(notional_filter.get("minNotional", notional_filter.get("notional", "0")), True),
        }
    except (KeyError, TypeError, ValueError):
        return None


class MarketService:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        scan_concurrency: int = 3,
        retries: int = 2,
    ):
        self._clock = clock
        self._client = client
        self._owns_client = client is None
        self._retries = max(0, min(4, retries))
        self._scan_concurrency = max(1, min(6, scan_concurrency))
        self.symbols: dict[str, list[dict[str, Any]]] = {"spot": [], "futures": []}
        self.signals: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._ticker: dict[str, dict[str, dict[str, Any]]] = {"spot": {}, "futures": {}}
        self._ticker_time = {"spot": 0, "futures": 0}
        self._universe_time = {"spot": 0, "futures": 0}
        self._funding: dict[str, float] = {}
        self._funding_time = 0
        self._cache: dict[tuple[str, str, str], CandleCache] = {}
        self._candle_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._ticker_locks = {market: asyncio.Lock() for market in PUBLIC_ENDPOINTS}
        self._funding_lock = asyncio.Lock()
        self._universe_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(8)
        self._limiters = {"spot": WeightedRateLimiter(1100), "futures": WeightedRateLimiter(2000)}
        self._funding_history_limiter = WeightedRateLimiter(400, 300)
        self._errors: dict[str, str | None] = {"spot": None, "futures": None}
        self._blocked_until = {"spot": 0.0, "futures": 0.0}
        self._scan_counts: dict[tuple[str, str], int] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._running = False
        self._started_at = 0

    def _now(self) -> int:
        return int(self._clock() * 1000)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if not self._owns_client:
                raise MarketUnavailable("عميل البيانات مغلق")
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15, connect=8),
                limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
                headers={"User-Agent": "NABD/1.0 public-market-data"},
                follow_redirects=False,
            )
        return self._client

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = self._now()
        self._http()
        self._tasks = [
            asyncio.create_task(self._universe_loop(), name="binance-universe"),
            asyncio.create_task(self._ticker_loop("spot"), name="binance-spot-tickers"),
            asyncio.create_task(self._ticker_loop("futures"), name="binance-futures-tickers"),
            asyncio.create_task(self._funding_loop(), name="binance-funding"),
        ]
        for market, horizons in HORIZONS.items():
            for horizon in horizons:
                self._tasks.append(asyncio.create_task(self._scan_loop(market, horizon), name=f"scan-{market}-{horizon}"))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _request(self, market: str, path: str, params: dict[str, Any] | None = None, weight: int = 1) -> Any:
        if market not in PUBLIC_ENDPOINTS:
            raise ValueError("invalid market")
        if self._clock() < self._blocked_until[market]:
            raise MarketUnavailable(self._errors[market] or "المصدر غير متاح حالياً")
        client = self._http()
        for attempt in range(self._retries + 1):
            await self._limiters[market].acquire(weight)
            if self._clock() < self._blocked_until[market]:
                raise MarketUnavailable(self._errors[market] or "المصدر غير متاح حالياً")
            try:
                async with self._semaphore:
                    response = await client.get(PUBLIC_ENDPOINTS[market] + path, params=params)
                self._limiters[market].observe(response.headers)
                if response.status_code in (451, 403):
                    message = "Binance غير متاح من موقع الخادم (HTTP %s). لا توجد بيانات بديلة أو أسعار افتراضية." % response.status_code
                    self._blocked_until[market] = self._clock() + 300
                    self._errors[market] = message
                    raise BinanceHTTPError(response.status_code, message)
                if response.status_code in (418, 429):
                    try:
                        retry_after = min(3600.0, max(1.0, float(response.headers.get("retry-after", "60"))))
                    except ValueError:
                        retry_after = 60.0
                    self._limiters[market].pause(retry_after)
                    self._blocked_until[market] = self._clock() + retry_after
                    message = "Binance طلب إيقاف الطلبات مؤقتاً بسبب حد البيانات (HTTP %s)." % response.status_code
                    self._errors[market] = message
                    raise BinanceHTTPError(response.status_code, message, retry_after)
                if response.status_code >= 500:
                    if attempt < self._retries:
                        await asyncio.sleep(min(4.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.2))
                        continue
                    raise BinanceHTTPError(response.status_code, "عطل مؤقت في مصدر Binance")
                if response.status_code >= 400:
                    raise BinanceHTTPError(response.status_code, "رفض Binance طلب البيانات (HTTP %s)" % response.status_code)
                try:
                    data = response.json()
                except ValueError as error:
                    raise MarketUnavailable("المصدر أعاد بيانات غير صالحة") from error
                if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
                    raise MarketUnavailable("المصدر رفض طلب البيانات")
                return data
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
                if attempt < self._retries:
                    await asyncio.sleep(min(4.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.2))
                    continue
                self._errors[market] = "تعذر الاتصال بمصدر Binance؛ تحقق من الشبكة وموقع الخادم"
                raise MarketUnavailable(self._errors[market]) from error
        raise MarketUnavailable("تعذر الحصول على البيانات")

    async def refresh_universe(self) -> None:
        async with self._universe_lock:
            results = await asyncio.gather(
                self._request("spot", "/api/v3/exchangeInfo", weight=20),
                self._request("futures", "/fapi/v1/exchangeInfo", weight=1),
                return_exceptions=True,
            )
            for market, result in zip(PUBLIC_ENDPOINTS, results):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    self._errors[market] = str(result)
                    continue
                if not isinstance(result, dict) or not isinstance(result.get("symbols"), list):
                    self._errors[market] = "قائمة عملات Binance غير صالحة"
                    continue
                metadata = [parsed for item in result["symbols"] if isinstance(item, dict) and (parsed := symbol_metadata(item, market)) is not None]
                metadata.sort(key=lambda item: (item["symbol"] != "BTCUSDT", item["symbol"]))
                live = {item["symbol"] for item in metadata}
                self.symbols[market] = metadata
                self._universe_time[market] = self._now()
                for key in list(self.signals):
                    if key[0] == market and key[2] not in live:
                        self.signals.pop(key, None)
                self._ticker[market] = {key: value for key, value in self._ticker[market].items() if key in live}
                for key in list(self._cache):
                    if key[0] == market and key[1] not in live:
                        self._cache.pop(key, None)
                        self._candle_locks.pop(key, None)
                if market == "futures":
                    self._funding = {key: value for key, value in self._funding.items() if key in live}
                self._errors[market] = None

    def _is_active(self, market: str, symbol: str) -> bool:
        return any(item["symbol"] == symbol for item in self.symbols.get(market, []))

    async def _refresh_tickers(self, market: str) -> None:
        async with self._ticker_locks[market]:
            if self._now() - self._ticker_time[market] < 8_000:
                return
            path = "/api/v3/ticker/24hr" if market == "spot" else "/fapi/v1/ticker/24hr"
            result = await self._request(market, path, weight=80 if market == "spot" else 40)
            if not isinstance(result, list):
                raise MarketUnavailable("بيانات الأسعار غير صالحة")
            live = {item["symbol"] for item in self.symbols[market]}
            tickers = {}
            for item in result:
                if not isinstance(item, dict) or item.get("symbol") not in live:
                    continue
                try:
                    change = float(item["priceChangePercent"])
                    if not math.isfinite(change):
                        raise ValueError("invalid price change")
                    tickers[item["symbol"]] = {
                        "price": _positive_number(item["lastPrice"]), "change24h": change,
                        "quoteVolume": _positive_number(item["quoteVolume"], True),
                        "high24h": _positive_number(item["highPrice"]),
                        "low24h": _positive_number(item["lowPrice"]),
                    }
                except (KeyError, ValueError, TypeError):
                    continue
            self._ticker[market] = tickers
            self._ticker_time[market] = self._now()
            self._errors[market] = None

    async def _refresh_funding(self) -> None:
        async with self._funding_lock:
            if self._funding_time and self._now() - self._funding_time < 30_000:
                return
            result = await self._request("futures", "/fapi/v1/premiumIndex", weight=10)
            if not isinstance(result, list):
                raise MarketUnavailable("بيانات التمويل غير صالحة")
            live = {item["symbol"] for item in self.symbols["futures"]}
            funding = {}
            for item in result:
                if not isinstance(item, dict) or item.get("symbol") not in live:
                    continue
                try:
                    value = float(item["lastFundingRate"])
                    if math.isfinite(value):
                        funding[item["symbol"]] = value
                except (KeyError, ValueError, TypeError):
                    continue
            self._funding = funding
            self._funding_time = self._now()

    async def historical_funding(self, symbol: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Public, point-in-time funding history for honest futures simulation.

        Binance applies a separate shared 500 requests/5-minute IP allowance
        to fundingRate/fundingInfo. Reserve 400 here; no alternate host bypass.
        """
        if not self._is_active("futures", symbol):
            raise MarketUnavailable("العملة غير متاحة في قائمة فيوتشر Binance")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("عدد سجلات التمويل يجب أن يكون بين 1 و1000")
        await self._funding_history_limiter.acquire(1)
        raw = await self._request("futures", "/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit}, weight=1)
        if not isinstance(raw, list):
            raise MarketUnavailable("سجل التمويل غير صالح")
        result = []
        for item in raw:
            try:
                funding_time = int(item["fundingTime"])
                funding_rate = str(item["fundingRate"])
                if item.get("symbol", symbol) != symbol or funding_time <= 0 or not math.isfinite(float(funding_rate)):
                    raise ValueError("invalid funding record")
                record: dict[str, Any] = {"symbol": symbol, "fundingTime": funding_time, "fundingRate": funding_rate}
                if item.get("markPrice") not in (None, ""):
                    _positive_number(item["markPrice"])
                    record["markPrice"] = str(item["markPrice"])
                result.append(record)
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise MarketUnavailable("سجل التمويل يحتوي بيانات غير صالحة") from error
        return sorted(result, key=lambda item: item["fundingTime"])

    async def _get_candles(self, market: str, symbol: str, interval: str, limit: int, for_analysis: bool = False) -> list[dict[str, Any]]:
        key = (market, symbol, interval)
        lock = self._candle_locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._now()
            cached = self._cache.get(key)
            if cached and cached.requested_limit >= limit and now < (cached.analysis_expires if for_analysis else cached.live_expires) and (for_analysis or not cached.closed_only):
                return cached.rows[-limit:]
            path = "/api/v3/klines" if market == "spot" else "/fapi/v1/klines"
            weight = 2 if market == "spot" else 1 if limit < 100 else 2 if limit < 500 else 5
            params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
            closed_end = _closed_end_time(interval, now) if for_analysis else None
            if closed_end is not None:
                # Asking the exchange for completed bars also protects against
                # an HTTP request that begins before a boundary and completes
                # after it. A sampled forming OHLC must never become closed.
                params["endTime"] = closed_end
            raw = await self._request(market, path, params, weight)
            if not isinstance(raw, list):
                raise MarketUnavailable("بيانات الشموع غير صالحة")
            rows = []
            try:
                for candle in raw:
                    if not isinstance(candle, list) or len(candle) < 7:
                        raise ValueError("invalid kline")
                    timestamp = int(candle[0])
                    close_time = int(candle[6])
                    values = {key: _positive_number(candle[index], key == "volume") for key, index in (("open", 1), ("high", 2), ("low", 3), ("close", 4), ("volume", 5))}
                    if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
                        raise ValueError("invalid OHLC")
                    if timestamp % 1000 or close_time != timestamp + INTERVAL_MS[interval] - 1:
                        raise ValueError("invalid kline timestamp")
                    rows.append({"time": timestamp // 1000, **values, "closeTime": close_time})
            except (IndexError, TypeError, ValueError, OverflowError) as error:
                raise MarketUnavailable("المصدر أعاد شموعاً غير صالحة") from error
            rows.sort(key=lambda candle: candle["time"])
            if len({candle["time"] for candle in rows}) != len(rows):
                raise MarketUnavailable("المصدر أعاد شموعاً مكررة")
            if closed_end is not None:
                rows = [candle for candle in rows if candle["closeTime"] <= closed_end]
            if not self._is_active(market, symbol):
                raise MarketUnavailable("العملة لم تعد متاحة في Binance")
            fetched_at = self._now()
            if closed_end is not None:
                analysis_expires = closed_end + INTERVAL_MS[interval] + 1
            elif rows:
                next_close = int(rows[-1]["closeTime"])
                # Use request-start time here: a bar that was forming when
                # the request began needs confirmation after its boundary,
                # even if network latency delivered that snapshot afterwards.
                if next_close < now:
                    next_close += INTERVAL_MS[interval]
                analysis_expires = next_close + 1
            else:
                analysis_expires = fetched_at + 60_000
            self._cache[key] = CandleCache(rows, limit, fetched_at + 10_000, analysis_expires, for_analysis)
            return rows

    def _validate(self, market: str, horizon: str | None = None) -> None:
        if market not in PUBLIC_ENDPOINTS:
            raise ValueError("القسم غير مدعوم")
        if horizon is not None and horizon not in HORIZONS[market]:
            raise ValueError("أفق التداول غير مدعوم لهذا القسم")

    async def candles(self, market: str, symbol: str, interval: str, limit: int = 500) -> list[dict[str, Any]]:
        self._validate(market)
        if interval not in ALLOWED_INTERVALS[market]:
            raise ValueError("الإطار الزمني غير مدعوم لهذا القسم")
        if not isinstance(limit, int) or not 2 <= limit <= 1000:
            raise ValueError("عدد الشموع يجب أن يكون بين 2 و1000")
        if not self._is_active(market, symbol):
            raise MarketUnavailable("العملة غير متاحة أو قائمة Binance لم تُحدّث بعد")
        rows = await self._get_candles(market, symbol, interval, limit)
        return [{key: candle[key] for key in ("time", "open", "high", "low", "close", "volume")} for candle in rows]

    def _btc_regime(self, market: str, horizon: str) -> str:
        interval = "1h" if market == "futures" else "1w" if horizon == "investment" else "1d"
        cached = self._cache.get((market, "BTCUSDT", interval))
        if cached is None:
            return "unknown"
        try:
            return regime_from_candles(cached.rows, interval, self._now())
        except AnalysisUnavailable:
            return "unknown"

    async def analyze_symbol(self, market: str, symbol: str, horizon: str) -> dict[str, Any]:
        self._validate(market, horizon)
        if not self._is_active(market, symbol):
            raise MarketUnavailable("العملة غير متاحة أو قائمة Binance لم تُحدّث بعد")
        now = self._now()
        if now - self._ticker_time[market] > 30_000:
            await self._refresh_tickers(market)
        if symbol not in self._ticker[market] or self._now() - self._ticker_time[market] > 120_000:
            raise AnalysisUnavailable("السعر والسيولة الحالية غير متاحين")
        required = REQUIRED_TIMEFRAMES[(market, horizon)]
        frames_raw = await asyncio.gather(*(self._get_candles(market, symbol, interval, 180, True) for interval in required))
        if symbol != "BTCUSDT" and self._is_active(market, "BTCUSDT"):
            await self._get_candles(market, "BTCUSDT", required[0], 180, True)
        if market == "futures" and self._now() - self._funding_time > 90_000:
            await self._refresh_funding()
        if not self._is_active(market, symbol):
            raise MarketUnavailable("العملة أزيلت من Binance أثناء التحليل")
        ticker = self._ticker[market].get(symbol)
        if ticker is None or self._now() - self._ticker_time[market] > 120_000:
            raise AnalysisUnavailable("السعر والسيولة الحالية غير متاحين")
        funding = self._funding.get(symbol) if self._now() - self._funding_time <= 120_000 else None
        analysis = analyze_candles(
            symbol, market, horizon, dict(zip(required, frames_raw)),
            ticker=ticker, btc_regime=self._btc_regime(market, horizon),
            funding_rate=funding if market == "futures" else None, now_ms=self._now(),
        )
        self.signals[(market, horizon, symbol)] = analysis
        return analysis

    async def snapshot(self, market: str, horizon: str) -> dict[str, Any]:
        self._validate(market, horizon)
        now = self._now()
        fresh_tickers = now - self._ticker_time[market] <= 120_000 and self._ticker_time[market] > 0
        items = []
        analyzed = 0
        for metadata in self.symbols[market]:
            symbol = metadata["symbol"]
            ticker = self._ticker[market].get(symbol) if fresh_tickers else None
            analysis = self.signals.get((market, horizon, symbol))
            if analysis and (analysis["validUntil"] <= now or not ticker):
                analysis = None
            if analysis:
                analyzed += 1
            item = {**metadata, **(ticker or {"price": None, "change24h": None, "quoteVolume": None, "high24h": None, "low24h": None}), "analysis": analysis}
            if market == "futures":
                item["fundingRate"] = self._funding.get(symbol) if now - self._funding_time <= 120_000 else None
            items.append(item)
        items.sort(key=lambda item: (-(item["analysis"]["score"] if item["analysis"] and item["analysis"]["side"] != "wait" else -1), -(item["quoteVolume"] or 0), item["symbol"]))
        total = len(items)
        connection = "live" if fresh_tickers and total and not self._errors[market] else "degraded" if fresh_tickers and total else "offline"
        return {
            "market": market, "horizon": horizon, "updatedAt": self._ticker_time[market] or None,
            "source": "binance", "connection": connection, "totalSymbols": total,
            "analyzedSymbols": analyzed,
            "scanProgress": round(min(100.0, self._scan_counts.get((market, horizon), 0) / total * 100), 1) if total else 0,
            "btcRegime": self._btc_regime(market, horizon), "items": items,
            "error": self._errors[market] or ("بانتظار بيانات Binance الحقيقية" if not fresh_tickers else None),
        }

    def status(self) -> dict[str, Any]:
        now = self._now()
        markets = {}
        for market in PUBLIC_ENDPOINTS:
            fresh = self._ticker_time[market] > 0 and now - self._ticker_time[market] <= 120_000
            markets[market] = {
                "connection": "live" if fresh and self.symbols[market] and not self._errors[market] else "degraded" if fresh else "offline",
                "totalSymbols": len(self.symbols[market]), "universeUpdatedAt": self._universe_time[market] or None,
                "tickerUpdatedAt": self._ticker_time[market] or None, "error": self._errors[market],
                "rateLimit": self._limiters[market].status(),
            }
        return {"running": self._running, "source": "binance", "startedAt": self._started_at or None, "markets": markets}

    async def _universe_loop(self) -> None:
        while self._running:
            try:
                await self.refresh_universe()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep the supervisor alive; endpoints keep the last honest
                # state, and the next refresh can recover after an outage.
                pass
            await asyncio.sleep(300)

    async def _ticker_loop(self, market: str) -> None:
        while self._running:
            try:
                if self.symbols[market]:
                    await self._refresh_tickers(market)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._errors[market] = str(error)
            await asyncio.sleep(15)

    async def _funding_loop(self) -> None:
        while self._running:
            try:
                if self.symbols["futures"]:
                    await self._refresh_funding()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._errors["futures"] = str(error)
            await asyncio.sleep(45)

    async def _scan_loop(self, market: str, horizon: str) -> None:
        """Visit every active symbol once per sweep, independently of score.

        BTC is seeded before the sweep to establish market context. Requests
        for overlapping spot horizons share a cache and per-frame lock.
        """
        while self._running:
            symbols = [item["symbol"] for item in self.symbols[market]]
            if not symbols or self._clock() < self._blocked_until[market]:
                await asyncio.sleep(5)
                continue
            self._scan_counts[(market, horizon)] = 0
            if "BTCUSDT" in symbols:
                try:
                    await self.analyze_symbol(market, "BTCUSDT", horizon)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.signals.pop((market, horizon, "BTCUSDT"), None)

            async def scan(symbol: str) -> None:
                if not self._is_active(market, symbol):
                    return
                try:
                    await self.analyze_symbol(market, symbol, horizon)
                except asyncio.CancelledError:
                    raise
                except AnalysisUnavailable:
                    self.signals.pop((market, horizon, symbol), None)
                except Exception as error:
                    self.signals.pop((market, horizon, symbol), None)
                    if isinstance(error, MarketUnavailable):
                        self._errors[market] = str(error)
                finally:
                    self._scan_counts[(market, horizon)] += 1

            for offset in range(0, len(symbols), self._scan_concurrency):
                if not self._running or self._clock() < self._blocked_until[market]:
                    break
                await asyncio.gather(*(scan(symbol) for symbol in symbols[offset:offset + self._scan_concurrency]))
                await asyncio.sleep(0)
            await asyncio.sleep(30 if market == "futures" else 60)
