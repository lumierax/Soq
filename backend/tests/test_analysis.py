"""Deterministic synthetic candles exercise math and rejection rules, not profit."""

import math

import pytest

from app.analysis import (
    AnalysisUnavailable, INTERVAL_MS, analyze_candles, closed_candles,
    ema, frame_indicators, regime_from_candles, rsi,
)


NOW = 1_760_000_000_000


def make_frame(interval, count=180, bearish=False):
    duration = INTERVAL_MS[interval]
    boundary = NOW // duration * duration
    close = 150.0 if bearish else 100.0
    rows = []
    pattern = [0.3, 0.3, -0.25, -0.25, 0.3]
    for index in range(count):
        open_price = close
        move = pattern[index % len(pattern)] + (0.7 if index == count - 1 else 0)
        close += -move if bearish else move
        rows.append({
            "time": (boundary - duration * (count - index)) // 1000,
            "open": open_price, "high": max(open_price, close) + 0.3,
            "low": min(open_price, close) - 0.3, "close": close,
            "volume": 150 if index == count - 1 else 100,
        })
    return rows


def setup_args(bearish=False):
    frames = {interval: make_frame(interval, bearish=bearish) for interval in ("1h", "15m", "3m")}
    return dict(
        symbol="TESTUSDT", market="futures", horizon="intraday", frames=frames,
        ticker={"price": frames["3m"][-1]["close"], "quoteVolume": 100_000_000},
        btc_regime="bearish" if bearish else "bullish", funding_rate=0.0001,
        now_ms=NOW,
    )


def test_rsi_wilder_reference_and_flat_series():
    reference = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert rsi(reference) == pytest.approx(70.46413502109705)
    assert rsi([10] * 30) == 50
    assert math.isnan(rsi([1, 2, 3]))
    assert ema([10, 10, 10], 21) == [10, 10, 10]


@pytest.mark.parametrize("bearish,side", [(False, "long"), (True, "short")])
def test_aligned_closed_frames_can_produce_a_setup(bearish, side):
    result = analyze_candles(**setup_args(bearish))
    assert result["side"] == side
    assert 70 <= result["score"] <= 100
    assert result["riskReward"] == pytest.approx(abs(result["targets"][0] - result["entry"]) / abs(result["entry"] - result["stop"]), abs=0.01)
    assert result["asOf"] < result["validUntil"]
    assert result["closedCandleTime"] < NOW
    assert result["validUntil"] - result["closedCandleTime"] <= INTERVAL_MS["3m"] + 1
    assert [item["interval"] for item in result["timeframes"]] == ["1h", "15m", "3m"]
    assert all(math.isfinite(item["rsi"]) and math.isfinite(item["adx"]) for item in result["timeframes"])
    assert "winProbability" not in result


def test_unclosed_extreme_candle_cannot_change_the_setup():
    args = setup_args()
    baseline = analyze_candles(**args)
    frame = args["frames"]["3m"]
    start = NOW // INTERVAL_MS["3m"] * INTERVAL_MS["3m"]
    frame.append({"time": start // 1000, "open": 110, "high": 10_000, "low": 1, "close": 9999, "volume": 10_000_000})
    assert analyze_candles(**args) == baseline
    assert len(closed_candles(frame, "3m", NOW)) == 180


@pytest.mark.parametrize("problem", ["missing", "short", "stale", "gap", "bad_ohlc", "nan"])
def test_bad_or_insufficient_frames_are_rejected(problem):
    args = setup_args()
    frame = args["frames"]["15m"]
    if problem == "missing":
        del args["frames"]["15m"]
    elif problem == "short":
        args["frames"]["15m"] = frame[-40:]
    elif problem == "stale":
        for candle in frame:
            candle["time"] -= 7 * INTERVAL_MS["15m"] // 1000
    elif problem == "gap":
        del frame[-20]
    elif problem == "bad_ohlc":
        frame[-1]["high"] = 1
    elif problem == "nan":
        frame[-1]["close"] = math.nan
    with pytest.raises(AnalysisUnavailable):
        analyze_candles(**args)


@pytest.mark.parametrize("condition,warning", [
    ("illiquid", "سيولة"), ("unknown_btc", "بيتكوين"),
    ("no_funding", "التمويل"), ("chase", "بعيداً"),
    ("weak_volume", "حجم"), ("unaligned", "تتوافق"),
])
def test_disqualifiers_never_force_a_trade(condition, warning):
    args = setup_args()
    if condition == "illiquid":
        args["ticker"]["quoteVolume"] = 1_000_000
    elif condition == "unknown_btc":
        args["btc_regime"] = "unknown"
    elif condition == "no_funding":
        args["funding_rate"] = None
    elif condition == "chase":
        args["ticker"]["price"] += 20
    elif condition == "weak_volume":
        args["frames"]["3m"][-1]["volume"] = 1
    elif condition == "unaligned":
        args["frames"]["1h"] = make_frame("1h", bearish=True)
    result = analyze_candles(**args)
    assert result["side"] == "wait"
    assert any(warning in item for item in result["warnings"])


def test_spot_never_recommends_a_short_and_requires_its_own_frames():
    frames = {interval: make_frame(interval, bearish=True) for interval in ("1d", "4h")}
    result = analyze_candles("TESTUSDT", "spot", "swing", frames, ticker={"quoteVolume": 100_000_000}, btc_regime="bearish", now_ms=NOW)
    assert result["side"] == "wait"
    assert result["trend"] == "bearish"
    with pytest.raises(AnalysisUnavailable):
        analyze_candles("TESTUSDT", "spot", "investment", frames, now_ms=NOW)


def test_btc_regime_cannot_use_stale_or_short_data():
    assert regime_from_candles(make_frame("1h"), "1h", NOW) == "bullish"
    assert regime_from_candles(make_frame("1h", count=30), "1h", NOW) == "unknown"
    assert regime_from_candles(make_frame("1h"), "1h", NOW + INTERVAL_MS["1h"] * 4) == "unknown"


def test_indicator_outputs_are_finite_and_directional():
    frame = make_frame("1h")
    values = frame_indicators(frame)
    assert values["trend"] == "bullish"
    assert values["atr"] > 0
    assert values["plusDI"] > values["minusDI"]
    assert 0 <= values["adx"] <= 100


def test_invalid_projected_price_levels_are_rejected_instead_of_shown():
    args = setup_args(bearish=True)
    args["ticker"]["price"] = 1
    with pytest.raises(AnalysisUnavailable):
        analyze_candles(**args)
