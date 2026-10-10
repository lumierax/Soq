"""Replay chronology, ambiguous bars and transaction cost verification."""
import pytest

from app import backtest
from app.analysis import AnalysisUnavailable, INTERVAL_MS


def history(interval, count, start=1_700_000_000_000):
    duration = INTERVAL_MS[interval]
    return [{"time": (start + i * duration) // 1000,
             "closeTime": start + (i + 1) * duration - 1,
             "open": 100.0, "close": 100.0, "high": 105.0, "low": 95.0,
             "volume": 1_000_000} for i in range(count)]


def test_replay_never_sees_future_and_takes_adverse_stop_first(monkeypatch):
    seen = []
    def deterministic_setup(symbol, market, horizon, frames, **kwargs):
        now = kwargs["now_ms"]
        assert all(c["closeTime"] < now for candles in frames.values() for c in candles)
        seen.append(now)
        return {"side": "long", "score": 90, "riskReward": 2.2,
                "stop": 96, "targets": [109, 112, 118]}
    monkeypatch.setattr(backtest, "analyze_candles", deterministic_setup)
    daily = history("1d", 122)
    four = history("4h", 732)
    # Each entry bar spans both stop and target, so an optimistic ordering
    # would manufacture a win. Replay must select the stop.
    for candle in four:
        candle["high"] = 110
    result = backtest.simulate({"1d": daily, "4h": four}, "spot", "BTCUSDT", "swing", daily,
                               now_ms=int(four[-1]["closeTime"]) + 1)
    assert result["tradeCount"] > 0
    assert result["winRate"] == 0
    assert all(t["reason"] == "stop" and t["pnl"] < 0 for t in result["trades"])
    assert result["trades"][0]["openedAt"] >= seen[0]
    assert result["trades"][0]["entry"] > 100  # adverse slippage
    assert result["netReturn"] < 0 and result["maxDrawdown"] > 0


def test_forming_and_future_bars_cannot_change_replay(monkeypatch):
    monkeypatch.setattr(backtest, "analyze_candles", lambda *args, **kwargs:
                        {"side": "wait", "score": 0, "riskReward": 0})
    daily = history("1d", 122)
    four = history("4h", 732)
    cutoff = int(four[-7]["closeTime"]) + 1
    complete = backtest.simulate({"1d": daily, "4h": four}, "spot", "BTCUSDT", "swing", daily, now_ms=cutoff)
    trimmed = {"1d": [c for c in daily if c["closeTime"] < cutoff],
               "4h": [c for c in four if c["closeTime"] < cutoff]}
    partial = backtest.simulate(trimmed, "spot", "BTCUSDT", "swing", trimmed["1d"], now_ms=cutoff)
    assert complete == partial
    assert complete["winRate"] is None and complete["tradeCount"] == 0


def test_insufficient_data_fails_honestly():
    with pytest.raises(AnalysisUnavailable):
        backtest.simulate({"1d": history("1d", 3), "4h": history("4h", 5)},
                          "spot", "BTCUSDT", "swing", history("1d", 3))


@pytest.mark.parametrize("missing_index", [600, 601], ids=["pending-entry-bar", "open-position-bar"])
def test_missing_execution_bar_rejects_sample_before_any_trading(monkeypatch, missing_index):
    """A gap must not delay an entry or hide stop/target/funding while held."""
    seen = []

    def setup(*args, **kwargs):
        seen.append(kwargs["now_ms"])
        return {"side": "long", "score": 90, "riskReward": 2.25,
                "stop": 96, "targets": [109, 112, 118]}

    monkeypatch.setattr(backtest, "analyze_candles", setup)
    daily = history("1d", 122)
    four = history("4h", 732)
    for candle in four:
        candle.update(high=103.0, low=97.0)
    now = int(four[-1]["closeTime"]) + 1
    baseline = backtest.simulate({"1d": daily, "4h": four}, "spot", "BTCUSDT", "swing", daily, now_ms=now)
    assert baseline["tradeCount"] == 1
    assert baseline["trades"][0]["openedAt"] == int(four[600]["time"] * 1000)
    assert baseline["trades"][0]["reason"] == "end_of_sample"
    seen.clear()
    incomplete = four[:missing_index] + four[missing_index + 1:]
    with pytest.raises(AnalysisUnavailable, match="سجل إطار التنفيذ يحتوي على فجوات"):
        backtest.simulate({"1d": daily, "4h": incomplete}, "spot", "BTCUSDT", "swing", daily, now_ms=now)
    assert not seen  # Reject before generating any recommendation or settlement result.
