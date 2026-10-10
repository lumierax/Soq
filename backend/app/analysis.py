"""Closed-candle, explainable technical analysis. Scores are not probabilities.

No market data or backtest performance is manufactured here. A missing frame,
an unfinished candle or a stale feed cannot produce an executable setup.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from typing import Any


STRATEGY_VERSION = "1.0"
MIN_CANDLES = 100
REQUIRED_TIMEFRAMES = {
    ("spot", "investment"): ("1w", "1d", "4h"),
    ("spot", "swing"): ("1d", "4h"),
    ("futures", "intraday"): ("1h", "15m", "3m"),
}
INTERVAL_MS = {
    "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
    "1d": 86_400_000, "1w": 604_800_000,
}


class AnalysisUnavailable(ValueError):
    """No honest current analysis can be produced from the provided data."""


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def wilder(values: Sequence[float], period: int) -> list[float]:
    """Wilder's smoothed average with an arithmetic-mean seed."""
    result = [math.nan] * len(values)
    if len(values) < period:
        return result
    result[period - 1] = sum(values[:period]) / period
    for index in range(period, len(values)):
        result[index] = (result[index - 1] * (period - 1) + values[index]) / period
    return result


def rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) <= period:
        return math.nan
    changes = [right - left for left, right in zip(values, values[1:])]
    gain = wilder([max(delta, 0.0) for delta in changes], period)[-1]
    loss = wilder([max(-delta, 0.0) for delta in changes], period)[-1]
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def atr_adx(candles: Sequence[Mapping[str, float]], period: int = 14) -> tuple[float, float, float, float]:
    true_ranges, plus_moves, minus_moves = [], [], []
    for prior, candle in zip(candles, candles[1:]):
        true_ranges.append(max(candle["high"] - candle["low"], abs(candle["high"] - prior["close"]), abs(candle["low"] - prior["close"])))
        up = candle["high"] - prior["high"]
        down = prior["low"] - candle["low"]
        plus_moves.append(up if up > down and up > 0 else 0.0)
        minus_moves.append(down if down > up and down > 0 else 0.0)
    atr_values = wilder(true_ranges, period)
    plus_values = wilder(plus_moves, period)
    minus_values = wilder(minus_moves, period)
    dx = []
    latest_plus = latest_minus = 0.0
    for true_range, plus, minus in zip(atr_values, plus_values, minus_values):
        if not math.isfinite(true_range):
            continue
        latest_plus = 100 * plus / true_range if true_range > 0 else 0.0
        latest_minus = 100 * minus / true_range if true_range > 0 else 0.0
        denominator = latest_plus + latest_minus
        dx.append(100 * abs(latest_plus - latest_minus) / denominator if denominator else 0.0)
    adx_values = wilder(dx, period)
    return atr_values[-1], adx_values[-1], latest_plus, latest_minus


def closed_candles(candles: Sequence[Mapping[str, Any]], interval: str, now_ms: int | None = None) -> list[dict[str, float]]:
    """Validate, sort and de-duplicate candles, excluding the forming candle."""
    if interval not in INTERVAL_MS:
        raise AnalysisUnavailable("إطار زمني غير مدعوم")
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    interval_ms = INTERVAL_MS[interval]
    result: dict[int, dict[str, float]] = {}
    for original in candles:
        try:
            timestamp = int(original["time"])
            close_time = int(original.get("closeTime", timestamp * 1000 + interval_ms - 1))
            if close_time >= now_ms:
                continue
            candle = {key: float(original[key]) for key in ("open", "high", "low", "close", "volume")}
            if not all(math.isfinite(value) for value in candle.values()):
                raise ValueError("non-finite candle")
            if min(candle[key] for key in ("open", "high", "low", "close")) <= 0 or candle["volume"] < 0:
                raise ValueError("invalid candle value")
            if candle["high"] < max(candle["open"], candle["close"], candle["low"]) or candle["low"] > min(candle["open"], candle["close"], candle["high"]):
                raise ValueError("invalid OHLC ordering")
            if close_time != timestamp * 1000 + interval_ms - 1:
                raise ValueError("invalid close time")
            candle.update(time=timestamp, closeTime=close_time)
            result[timestamp] = candle
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise AnalysisUnavailable("بيانات شموع غير صالحة") from error
    return [result[key] for key in sorted(result)]


def frame_indicators(candles: Sequence[Mapping[str, float]]) -> dict[str, float | str]:
    closes = [candle["close"] for candle in candles]
    fast = ema(closes, 21)
    slow = ema(closes, 55)
    ema12, ema26 = ema(closes, 12), ema(closes, 26)
    macd = [left - right for left, right in zip(ema12, ema26)]
    signal = ema(macd, 9)
    histogram = macd[-1] - signal[-1]
    atr_value, adx_value, plus_di, minus_di = atr_adx(candles)
    slope = (slow[-1] - slow[-6]) / slow[-6]
    trend = "neutral"
    if closes[-1] > fast[-1] > slow[-1] and slope > 0:
        trend = "bullish"
    elif closes[-1] < fast[-1] < slow[-1] and slope < 0:
        trend = "bearish"
    baseline_volume = sum(candle["volume"] for candle in candles[-21:-1]) / 20
    volume_ratio = candles[-1]["volume"] / baseline_volume if baseline_volume > 0 else 0.0
    return {
        "trend": trend, "rsi": rsi(closes), "adx": adx_value,
        "atr": atr_value, "ema21": fast[-1], "ema55": slow[-1],
        "macd": macd[-1], "macdHistogram": histogram,
        "plusDI": plus_di, "minusDI": minus_di,
        "volumeRatio": volume_ratio, "slope": slope,
    }


def regime_from_candles(candles: Sequence[Mapping[str, Any]], interval: str, now_ms: int | None = None) -> str:
    values = closed_candles(candles, interval, now_ms)
    if len(values) < MIN_CANDLES:
        return "unknown"
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if now_ms >= values[-1]["closeTime"] + INTERVAL_MS[interval] + 1:
        return "unknown"
    return str(frame_indicators(values)["trend"])


def analyze_candles(
    symbol: str,
    market: str,
    horizon: str,
    frames: Mapping[str, Sequence[Mapping[str, Any]]],
    ticker: Mapping[str, Any] | None = None,
    btc_regime: str = "unknown",
    funding_rate: float | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build a current setup using aligned frames and explicit disqualifiers.

    Strength weights: alignment 32, ADX 14, RSI 12, MACD 12, volume 10,
    price structure 10, BTC regime 5 and liquidity 5. Their sum is a ranking
    score, never an estimated likelihood of profit. Risk/return is geometric.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    required = REQUIRED_TIMEFRAMES.get((market, horizon))
    if required is None:
        raise AnalysisUnavailable("قسم أو أفق تداول غير مدعوم")
    validated = {}
    indicators = {}
    for interval in required:
        if interval not in frames:
            raise AnalysisUnavailable(f"بيانات {interval} غير متاحة")
        values = closed_candles(frames[interval], interval, now_ms)
        if len(values) < MIN_CANDLES:
            raise AnalysisUnavailable(f"يلزم {MIN_CANDLES} شمعة مغلقة على {interval}")
        if now_ms >= values[-1]["closeTime"] + INTERVAL_MS[interval] + 1:
            raise AnalysisUnavailable(f"بيانات {interval} قديمة")
        recent = values[-MIN_CANDLES:]
        if any(right["time"] - left["time"] != INTERVAL_MS[interval] // 1000 for left, right in zip(recent, recent[1:])):
            raise AnalysisUnavailable(f"شموع {interval} غير متصلة")
        validated[interval] = values
        indicators[interval] = frame_indicators(values)
    execution_interval = required[-1]
    execution = indicators[execution_interval]
    candles = validated[execution_interval]
    close = candles[-1]["close"]
    ticker = ticker or {}
    try:
        entry = float(ticker.get("price", close))
        quote_volume = float(ticker.get("quoteVolume", 0))
        if not math.isfinite(entry) or entry <= 0 or not math.isfinite(quote_volume) or quote_volume < 0:
            raise ValueError("invalid ticker")
    except (TypeError, ValueError) as error:
        raise AnalysisUnavailable("بيانات السعر والسيولة غير صالحة") from error
    atr_value = float(execution["atr"])
    if atr_value <= 0 or not math.isfinite(atr_value):
        raise AnalysisUnavailable("التذبذب غير قابل للتحليل")
    bullish = sum(item["trend"] == "bullish" for item in indicators.values())
    bearish = sum(item["trend"] == "bearish" for item in indicators.values())
    direction = "long" if bullish > bearish else "short" if bearish > bullish else "wait"
    if market == "spot" and direction == "short":
        direction = "wait"
    trend = "bullish" if bullish == len(required) else "bearish" if bearish == len(required) else "neutral"
    reasons: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    score = 0.0
    aligned = bullish if direction == "long" else bearish if direction == "short" else 0
    score += 32 * aligned / len(required)
    if aligned == len(required):
        reasons.append("الاتجاه متوافق عبر جميع الأطر")
    else:
        blockers.append("الأطر الزمنية لم تتوافق بعد")
    adx_value = float(execution["adx"])
    score += 14 * max(0.0, min(1.0, (adx_value - 12) / 18))
    if adx_value >= 22:
        reasons.append("قوة اتجاه واضحة بحسب ADX")
    elif adx_value < 16:
        blockers.append("اتجاه ضعيف أو سوق عرضي")
    momentum = float(execution["rsi"])
    rsi_ok = 48 <= momentum <= 74 if direction == "long" else 26 <= momentum <= 52
    if rsi_ok:
        score += 12
        reasons.append("الزخم يدعم الاتجاه دون تشبع شديد")
    elif momentum >= 80 or momentum <= 20:
        blockers.append("تشبع شديد؛ انتظار تهدئة الزخم")
    else:
        warnings.append("الزخم يحتاج تأكيداً إضافياً")
    macd_ok = float(execution["macd"]) > 0 and float(execution["macdHistogram"]) >= 0 if direction == "long" else float(execution["macd"]) < 0 and float(execution["macdHistogram"]) <= 0
    if direction != "wait" and macd_ok:
        score += 12
        reasons.append("MACD يؤكد الزخم")
    else:
        warnings.append("MACD لا يؤكد استمرار الحركة")
    volume_ratio = float(execution["volumeRatio"])
    score += 10 * min(1.0, max(0.0, volume_ratio - 0.5) / 0.8)
    if volume_ratio >= 1.2:
        reasons.append("الحركة مدعومة بحجم تداول مرتفع")
    elif volume_ratio < 0.65:
        blockers.append("حجم التداول ضعيف")
    else:
        warnings.append("حجم التداول قريب من المعتاد")
    prior = candles[-21:-1]
    upper = max(candle["high"] for candle in prior)
    lower = min(candle["low"] for candle in prior)
    breakout = close > upper if direction == "long" else close < lower
    pullback = abs(close - float(execution["ema21"])) <= 1.2 * atr_value
    if direction != "wait" and (breakout or pullback):
        score += 10
        reasons.append("اختراق بنية سعرية" if breakout else "السعر قريب من منطقة إعادة اختبار الاتجاه")
    else:
        warnings.append("السعر بعيد عن منطقة دخول واضحة")
    matching_regime = "bullish" if direction == "long" else "bearish"
    if btc_regime == matching_regime:
        score += 5
        reasons.append("اتجاه بيتكوين يدعم الإعداد")
    elif btc_regime == "unknown":
        blockers.append("سياق بيتكوين غير متاح أو قديم")
    elif btc_regime not in ("neutral", matching_regime) and symbol != "BTCUSDT":
        warnings.append("اتجاه بيتكوين معاكس؛ احتمال انعكاس أعلى")
        score -= 8
    min_volume = 20_000_000 if market == "futures" else 5_000_000
    if quote_volume >= min_volume:
        score += 5
        reasons.append("سيولة يومية كافية مبدئياً")
    else:
        blockers.append("سيولة USDT اليومية أقل من حد الاستراتيجية")
    if market == "futures":
        if funding_rate is None or not math.isfinite(funding_rate):
            blockers.append("بيانات التمويل غير متاحة")
        elif (direction == "long" and funding_rate > 0.0008) or (direction == "short" and funding_rate < -0.0008):
            warnings.append("تمويل مرتفع ضد جهة الصفقة")
            score -= 10
    if abs(entry - close) > atr_value:
        blockers.append("تحرك السعر بعيداً عن آخر شمعة؛ يلزم تأكيد جديد")
    atr_percent = atr_value / entry * 100
    if atr_percent > (5 if market == "futures" else 14):
        blockers.append("تذبذب استثنائي؛ تكلفة الوقف غير مستقرة")
    short = direction == "short"
    pivot = max(candle["high"] for candle in candles[-12:]) if short else min(candle["low"] for candle in candles[-12:])
    stop = max(pivot + atr_value * 0.25, entry + atr_value * 1.4) if short else min(pivot - atr_value * 0.25, entry - atr_value * 1.4)
    risk = abs(entry - stop)
    if stop <= 0:
        raise AnalysisUnavailable("لا يمكن بناء وقف صالح من البنية والتذبذب الحاليين")
    if risk > atr_value * 5:
        blockers.append("وقف البنية بعيد؛ انتظار دخول أفضل")
    sign = -1 if short else 1
    target1 = entry + sign * risk * 2.2
    barriers = [candle["low"] if short else candle["high"] for candle in candles[-80:-1]]
    viable = [value for value in barriers if target1 < value < entry - risk * 0.5] if short else [value for value in barriers if entry + risk * 0.5 < value < target1]
    if viable:
        target1 = max(viable) if short else min(viable)
        warnings.append("منطقة عرض أو طلب تاريخية تحد الهدف الأول")
    risk_reward = abs(target1 - entry) / risk if risk else 0.0
    if risk_reward < 1.8:
        blockers.append("العائد إلى المخاطرة غير كافٍ قبل منطقة العرض أو الطلب")
    targets = [target1, entry + sign * risk * 3.3, entry + sign * risk * 4.5]
    if any(value <= 0 for value in targets):
        raise AnalysisUnavailable("لا يمكن بناء أهداف صالحة من البنية والتذبذب الحاليين")
    score = int(round(max(0.0, min(100.0, score))))
    if score < 70:
        blockers.append("قوة الإعداد أقل من حد التفعيل")
    side = direction if direction != "wait" and not blockers else "wait"
    warnings.extend(blockers)
    # A future closed execution candle invalidates the setup, even if the UI
    # hasn't completed its next universe-wide scan yet.
    close_time = int(candles[-1]["closeTime"])
    valid_until = min(now_ms + INTERVAL_MS[execution_interval], close_time + INTERVAL_MS[execution_interval] + 1)
    return {
        "symbol": symbol, "market": market, "horizon": horizon,
        "asOf": now_ms, "validUntil": valid_until,
        "side": side, "score": score, "entry": entry, "stop": stop,
        "targets": targets, "riskReward": round(risk_reward, 2),
        "atrPercent": round(atr_percent, 3), "trend": trend,
        "reasons": reasons, "warnings": list(dict.fromkeys(warnings)),
        "timeframes": [{"interval": interval, "trend": indicators[interval]["trend"], "rsi": round(float(indicators[interval]["rsi"]), 1), "adx": round(float(indicators[interval]["adx"]), 1)} for interval in required],
        "strategyVersion": STRATEGY_VERSION, "closedCandleTime": close_time,
    }
