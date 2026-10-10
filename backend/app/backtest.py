"""Historical, closed-candle evaluation without future information.

This is an exploratory single-symbol simulation, not a certified execution
model or a claimed win rate. Entry occurs at the following candle's open.
"""
from __future__ import annotations

import asyncio
import bisect
import math
import time
from typing import Any

from .analysis import (
    AnalysisUnavailable, INTERVAL_MS, MIN_CANDLES, REQUIRED_TIMEFRAMES,
    analyze_candles, closed_candles, regime_from_candles,
)


def simulate(
    frames: dict[str, list[dict]], market: str, symbol: str, horizon: str,
    btc_frames: list[dict], funding: list[dict] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Replay one symbol with 1% risk, no leverage and capped 1x notional."""
    required = REQUIRED_TIMEFRAMES.get((market, horizon))
    if not required:
        raise AnalysisUnavailable("أفق الاختبار غير مدعوم")
    now_ms = now_ms or int(time.time() * 1000)
    cleaned = {frame: closed_candles(frames.get(frame, []), frame, now_ms) for frame in required}
    base_interval, regime_interval = required[-1], required[0]
    base = cleaned[base_interval]
    btc = closed_candles(btc_frames, regime_interval, now_ms)
    if any(len(values) < MIN_CANDLES + 2 for values in cleaned.values()) or len(btc) < MIN_CANDLES:
        raise AnalysisUnavailable("العينة التاريخية غير كافية للاختبار متعدد الأطر")
    # Execution cannot bridge an unobserved bar: an expired setup could enter
    # late, or a held position could miss a stop, target or funding payment.
    # Reject the sample rather than inventing the price path inside the gap.
    if any(int(right["closeTime"]) - int(left["closeTime"]) != INTERVAL_MS[base_interval]
           for left, right in zip(base, base[1:])):
        raise AnalysisUnavailable("سجل إطار التنفيذ يحتوي على فجوات؛ لا يمكن إصدار نتيجة تاريخية موثوقة")
    times = {interval: [int(c["closeTime"]) for c in values] for interval, values in cleaned.items()}
    btc_times = [int(c["closeTime"]) for c in btc]
    funding = sorted(funding or [], key=lambda item: int(item["fundingTime"]))
    funding_times = [int(item["fundingTime"]) for item in funding]
    fees_bps = 10 if market == "spot" else 5
    fee = fees_bps / 10_000
    slippage = 2 / 10_000
    equity = initial = peak = 10_000.0
    max_drawdown = 0.0
    trades: list[dict] = []
    position: dict | None = None
    pending: dict | None = None
    eligible = evaluated = skipped = 0
    sample_start: int | None = None

    def close_trade(raw_exit: float, candle: dict, reason: str) -> None:
        nonlocal position, equity
        assert position is not None
        sign = position["sign"]
        execution = raw_exit * (1 - sign * slippage)
        gross = sign * (execution - position["entry"]) * position["quantity"]
        exit_fee = execution * position["quantity"] * fee
        equity += gross - exit_fee
        net = gross - exit_fee - position["entryFee"] + position["fundingPnl"]
        trades.append({
            "side": "long" if sign == 1 else "short", "entry": position["entry"],
            "exit": execution, "stop": position["stop"], "target": position["target"],
            "openedAt": position["openedAt"], "closedAt": int(candle["closeTime"]),
            "pnl": round(net, 4), "reason": reason,
            "fundingPnl": round(position["fundingPnl"], 4),
        })
        position = None

    for index in range(MIN_CANDLES, len(base)):
        candle = base[index]
        open_time = int(candle["time"] * 1000)
        decision_time = int(candle["closeTime"]) + 1
        if pending and position is None:
            sign = 1 if pending["side"] == "long" else -1
            entry = candle["open"] * (1 + sign * slippage)
            stop, target = pending["stop"], pending["targets"][0]
            distance = sign * (entry - stop)
            reward = sign * (target - entry)
            # Gaps or poor post-fill geometry cancel rather than retroactively
            # moving the entry price or the stop to manufacture a good trade.
            if distance > 0 and reward / distance >= 1.8 and equity > 0:
                quantity = min(equity * 0.01 / distance, equity / entry)
                entry_fee = quantity * entry * fee
                equity -= entry_fee
                position = {"entry": entry, "stop": stop, "target": target,
                            "quantity": quantity, "sign": sign, "entryFee": entry_fee,
                            "openedAt": open_time, "fundingPnl": 0.0}
            pending = None

        if position:
            sign = position["sign"]
            stop_hit = candle["low"] <= position["stop"] if sign == 1 else candle["high"] >= position["stop"]
            target_hit = candle["high"] >= position["target"] if sign == 1 else candle["low"] <= position["target"]
            # Adverse stop gaps fill at the open; ambiguous intrabar ordering
            # takes the stop first. The target is always a conservative fill.
            gap_stop = candle["open"] <= position["stop"] if sign == 1 else candle["open"] >= position["stop"]
            fstart = bisect.bisect_left(funding_times, max(open_time, position["openedAt"]))
            fend = bisect.bisect_right(funding_times, int(candle["closeTime"]))
            # If an exit and a funding event share a candle, charge an adverse
            # payment but don't credit a favorable payment: sequence unknown.
            for record in funding[fstart:fend]:
                mark = float(record.get("markPrice") or candle["open"])
                payment = -sign * float(record["fundingRate"]) * mark * position["quantity"]
                if (stop_hit or target_hit) and payment > 0:
                    payment = 0
                equity += payment
                position["fundingPnl"] += payment
            if stop_hit:
                close_trade(candle["open"] if gap_stop else position["stop"], candle, "stop")
            elif target_hit:
                close_trade(position["target"], candle, "target")

        mark_equity = equity
        if position:
            mark_equity += position["sign"] * (candle["close"] - position["entry"]) * position["quantity"]
            mark_equity -= candle["close"] * position["quantity"] * fee
        peak = max(peak, mark_equity)
        max_drawdown = max(max_drawdown, (peak - mark_equity) / peak * 100)
        if equity <= 0 or position or index == len(base) - 1:
            continue

        aligned = {}
        for interval in required:
            end = bisect.bisect_left(times[interval], decision_time)
            aligned[interval] = cleaned[interval][max(0, end - 280):end]
        btc_end = bisect.bisect_left(btc_times, decision_time)
        historical_btc = btc[max(0, btc_end - 280):btc_end]
        if any(len(values) < MIN_CANDLES for values in aligned.values()) or len(historical_btc) < MIN_CANDLES:
            skipped += 1
            continue
        sample_start = sample_start or decision_time
        eligible += 1
        # Estimated point-in-time daily quote volume from historical OHLCV,
        # rather than today's ticker (which would introduce look-ahead).
        daily_count = max(1, 86_400_000 // INTERVAL_MS[base_interval])
        daily = base[max(0, index + 1 - daily_count):index + 1]
        quote_volume = sum(c["volume"] * c["close"] for c in daily)
        funding_rate = None
        f_index = bisect.bisect_right(funding_times, decision_time) - 1
        if f_index >= 0 and decision_time - funding_times[f_index] <= 86_400_000:
            funding_rate = float(funding[f_index]["fundingRate"])
        try:
            setup = analyze_candles(
                symbol, market, horizon, aligned,
                ticker={"price": candle["close"], "quoteVolume": quote_volume},
                btc_regime=regime_from_candles(historical_btc, regime_interval, decision_time),
                funding_rate=funding_rate, now_ms=decision_time,
            )
            evaluated += 1
            if setup["side"] in ("long", "short") and setup["score"] >= 75 and setup["riskReward"] >= 2:
                pending = setup
        except AnalysisUnavailable:
            skipped += 1

    if position:
        close_trade(base[-1]["close"], base[-1], "end_of_sample")
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
    wins = [trade["pnl"] for trade in trades if trade["pnl"] > 0]
    losses = [-trade["pnl"] for trade in trades if trade["pnl"] < 0]
    limitations = [
        "اختبار تاريخي استكشافي لعملة واحدة؛ ليس احتمال نجاح أو ضماناً للأداء القادم.",
        "دخول عند افتتاح الشمعة التالية؛ وقف أولاً إذا لمس السعر الوقف والهدف في الشمعة نفسها.",
        "مخاطرة 1%، دون رافعة، حد قيمة الصفقة كامل رأس المال، واحتساب العمولة وانزلاق 2 نقطة أساس لكل جهة.",
        "السيولة تقديرية من OHLCV التاريخي؛ لا يحاكي دفتر الأوامر أو تأخر الشبكة أو تغير قوائم Binance.",
        "لا يشمل الأخبار أو السحب والإيداع أو إدارة محفظة متعددة العملات أو اختباراً مستقلاً خارج العينة.",
    ]
    if market == "futures":
        limitations.append("التمويل مأخوذ من التاريخ المتاح؛ لا تتولد إشارة عند غياب سياق التمويل السابق.")
    if len(trades) < 30:
        limitations.append("عدد الصفقات أقل من 30؛ العينة لا تكفي لاستنتاج موثوق عن أداء الاستراتيجية.")
    if horizon == "investment":
        limitations.append("مدة شموع 4 ساعات المتاحة قصيرة لتقييم الاستثمار طويل الأجل.")
    return {
        "symbol": symbol, "market": market, "horizon": horizon,
        "strategyVersion": "1.0", "trades": trades, "tradeCount": len(trades),
        "winRate": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "netReturn": round((equity / initial - 1) * 100, 3),
        "maxDrawdown": round(max_drawdown, 3),
        "profitFactor": round(sum(wins) / sum(losses), 3) if losses else None,
        "sampleStart": sample_start or int(base[0]["time"] * 1000),
        "sampleEnd": int(base[-1]["closeTime"]), "feesBps": fees_bps,
        "slippageBps": 2, "evaluatedCandles": evaluated, "skippedCandles": skipped,
        "eligibleCandles": eligible, "limitations": limitations,
    }


async def run_backtest(service: Any, market: str, symbol: str, horizon: str) -> dict:
    required = REQUIRED_TIMEFRAMES.get((market, horizon))
    if not required:
        raise AnalysisUnavailable("أفق الاختبار غير مدعوم")
    values = await asyncio.gather(*(service.candles(market, symbol, interval, 1000) for interval in required))
    btc = values[0] if symbol == "BTCUSDT" else await service.candles(market, "BTCUSDT", required[0], 1000)
    funding = await service.historical_funding(symbol, 1000) if market == "futures" else []
    return await asyncio.to_thread(simulate, dict(zip(required, values)), market, symbol, horizon, btc, funding)
