"""Compare the actual TypeScript implementation against the Python reference.

Run after npm ci: python3 frontend/tests/local-analysis-parity.py
Uses existing tsc + Node, compiles into a temporary directory, and makes no
network requests. Synthetic fixtures test parity, never claimed performance.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.analysis import (  # noqa: E402
    AnalysisUnavailable, INTERVAL_MS, REQUIRED_TIMEFRAMES, analyze_candles,
    closed_candles, ema, frame_indicators, regime_from_candles, rsi, wilder,
)

NOW = 1_760_000_000_000


def frame(interval: str, *, count=180, bearish=False, seed=None) -> list[dict]:
    duration = INTERVAL_MS[interval]
    offset = INTERVAL_MS["1d"] * 4 if interval == "1w" else 0
    boundary = (NOW - offset) // duration * duration + offset
    close = 150.0 if bearish else 100.0
    pattern = [0.3, 0.3, -0.25, -0.25, 0.3]
    generator = random.Random(seed) if seed is not None else None
    result = []
    for index in range(count):
        opening = close
        move = pattern[index % len(pattern)] + (0.7 if index == count - 1 else 0)
        if generator is not None:
            move = generator.uniform(-0.8, 0.85)
        close += -move if bearish else move
        wick = generator.uniform(0.05, 0.9) if generator is not None else 0.3
        volume = generator.uniform(0, 100_000) if generator is not None else 150.0 if index == count - 1 else 100.0
        result.append({
            "time": (boundary - duration * (count - index)) // 1000,
            "open": opening, "high": max(opening, close) + wick,
            "low": min(opening, close) - wick, "close": close, "volume": volume,
        })
    return result


def arguments(market="futures", horizon="intraday", *, bearish=False, seed=None) -> dict:
    required = REQUIRED_TIMEFRAMES[(market, horizon)]
    frames = {interval: frame(interval, bearish=bearish, seed=None if seed is None else seed + index) for index, interval in enumerate(required)}
    return {
        "symbol": "TESTUSDT", "market": market, "horizon": horizon, "frames": frames,
        "ticker": {"price": frames[required[-1]][-1]["close"], "quoteVolume": 100_000_000},
        "btcRegime": "bearish" if bearish else "bullish", "fundingRate": 0.0001,
        "nowMs": NOW,
    }


def python_result(operation: dict) -> dict:
    try:
        op = operation["op"]
        if op == "analysis":
            values = operation["args"].copy()
            values["btc_regime"] = values.pop("btcRegime", "unknown")
            values["funding_rate"] = values.pop("fundingRate", None)
            values["now_ms"] = values.pop("nowMs", NOW)
            value = analyze_candles(**values)
        elif op == "regime":
            value = regime_from_candles(operation["candles"], operation["interval"], operation["nowMs"])
        elif op == "closed":
            value = closed_candles(operation["candles"], operation["interval"], operation["nowMs"])
        elif op == "frame":
            value = frame_indicators(operation["candles"])
        elif op == "rsi":
            value = rsi(operation["values"], operation.get("period", 14))
        elif op == "ema":
            value = ema(operation["values"], operation["period"])
        elif op == "wilder":
            value = wilder(operation["values"], operation["period"])
        elif op == "round":
            value = round(operation["value"], operation["digits"])
        else:
            raise AssertionError("Unsupported operation")
        return {"ok": True, "value": value}
    except AnalysisUnavailable as error:
        return {"ok": False, "error": str(error), "name": "AnalysisUnavailable"}


def sanitized(value, *, nan_marker=False):
    if isinstance(value, float) and not math.isfinite(value):
        return "__NABD_NAN__" if nan_marker else None
    if isinstance(value, dict):
        return {key: sanitized(item, nan_marker=nan_marker) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitized(item, nan_marker=nan_marker) for item in value]
    return value


class LocalAnalysisParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nabd-analysis-parity-")
        cls.compiled = Path(cls.temp.name)
        subprocess.run([
            "node", str(ROOT / "frontend/node_modules/typescript/bin/tsc"),
            str(ROOT / "frontend/src/local-analysis.ts"), "--target", "ES2022",
            "--module", "commonjs", "--moduleResolution", "node", "--strict",
            "--skipLibCheck", "--outDir", str(cls.compiled),
        ], check=True, cwd=ROOT / "frontend", capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def assert_tree(self, actual, expected, path="result"):
        if isinstance(expected, dict):
            self.assertEqual(set(actual), set(expected), path)
            for key in expected:
                self.assert_tree(actual[key], expected[key], f"{path}.{key}")
        elif isinstance(expected, list):
            self.assertEqual(len(actual), len(expected), path)
            for index, (left, right) in enumerate(zip(actual, expected)):
                self.assert_tree(left, right, f"{path}[{index}]")
        elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
            self.assertIsInstance(actual, (float, int), path)
            self.assertTrue(math.isclose(actual, expected, rel_tol=1e-13, abs_tol=1e-12), f"{path}: {actual} != {expected}")
        else:
            self.assertEqual(actual, expected, path)

    def verify(self, operations):
        expected = [sanitized(python_result(operation)) for operation in operations]
        process = subprocess.run([
            "node", str(ROOT / "frontend/tests/local-analysis-runner.mjs"),
            str(self.compiled / "local-analysis.js"),
        ], input=json.dumps(sanitized(operations, nan_marker=True), allow_nan=False), text=True,
            capture_output=True, check=True, cwd=ROOT)
        actual = json.loads(process.stdout)
        self.assertEqual(len(actual), len(expected))
        for index, (left, right) in enumerate(zip(actual, expected)):
            with self.subTest(case=operations[index].get("label", index)):
                self.assert_tree(left, right)
        return actual

    def test_real_engine_output_and_error_parity(self):
        operations = []
        def add(label, args):
            operations.append({"op": "analysis", "label": label, "args": args})
        for market, horizon in (("futures", "intraday"), ("spot", "swing"), ("spot", "investment")):
            for bearish in (False, True):
                add(f"{market}/{horizon}/{'bearish' if bearish else 'bullish'}", arguments(market, horizon, bearish=bearish))
        for condition in ("illiquid", "btc_missing", "btc_against", "funding_missing", "funding_extreme", "chase", "weak_volume", "unaligned", "missing", "short", "stale", "gap", "bad_ohlc", "nan", "bad_close", "null_ticker", "impossible_targets", "forming", "dedup_sorted"):
            args = arguments(bearish=condition == "impossible_targets")
            frames = args["frames"]
            if condition == "illiquid": args["ticker"]["quoteVolume"] = 1_000
            elif condition == "btc_missing": args["btcRegime"] = "unknown"
            elif condition == "btc_against": args["btcRegime"] = "bearish"
            elif condition == "funding_missing": args["fundingRate"] = None
            elif condition == "funding_extreme": args["fundingRate"] = 0.004
            elif condition == "chase": args["ticker"]["price"] += 20
            elif condition == "weak_volume": frames["3m"][-1]["volume"] = 1
            elif condition == "unaligned": frames["1h"] = frame("1h", bearish=True)
            elif condition == "missing": del frames["15m"]
            elif condition == "short": frames["15m"] = frames["15m"][-99:]
            elif condition == "stale": frames["15m"].pop()
            elif condition == "gap": del frames["15m"][-20]
            elif condition == "bad_ohlc": frames["15m"][-1]["high"] = 1
            elif condition == "nan": frames["15m"][-1]["close"] = math.nan
            elif condition == "bad_close": frames["15m"][-1]["closeTime"] = frames["15m"][-1]["time"] * 1000 + 10
            elif condition == "null_ticker": args["ticker"]["price"] = None
            elif condition == "impossible_targets": args["ticker"]["price"] = 1
            elif condition == "forming":
                beginning = NOW // INTERVAL_MS["3m"] * INTERVAL_MS["3m"]
                frames["3m"].append({"time": beginning // 1000, "open": 100, "high": 9999, "low": 1, "close": math.nan, "volume": 100000})
            elif condition == "dedup_sorted":
                frames["3m"] = list(reversed(frames["3m"] + [copy.deepcopy(frames["3m"][-1])]))
            add(condition, args)
        add("invalid_horizon", {**arguments(), "horizon": "investment"})
        result = self.verify(operations)
        self.assertEqual(result[0]["value"]["side"], "long")
        self.assertEqual(result[1]["value"]["side"], "short")
        self.assertEqual(result[3]["value"]["side"], "wait")
        self.assertNotIn("winProbability", result[0]["value"])

    def test_randomized_indicator_and_setup_parity(self):
        operations = []
        for seed in range(40):
            market, horizon = ("futures", "intraday") if seed % 2 else ("spot", "swing")
            args = arguments(market, horizon, bearish=bool(seed % 3), seed=seed)
            operations.append({"op": "analysis", "label": f"random-analysis-{seed}", "args": args})
            operations.append({"op": "frame", "label": f"random-indicators-{seed}", "candles": args["frames"][REQUIRED_TIMEFRAMES[(market, horizon)][-1]]})
        self.verify(operations)

    def test_closed_boundary_and_regime_parity(self):
        operations = []
        for interval in INTERVAL_MS:
            candles = frame(interval)
            for now in (NOW, candles[-1]["time"] * 1000 + INTERVAL_MS[interval] - 1,
                        candles[-1]["time"] * 1000 + 2 * INTERVAL_MS[interval]):
                operations.append({"op": "closed", "candles": candles, "interval": interval, "nowMs": now})
                operations.append({"op": "regime", "candles": candles, "interval": interval, "nowMs": now})
        operations.append({"op": "regime", "candles": frame("1h", count=30), "interval": "1h", "nowMs": NOW})
        operations.append({"op": "closed", "candles": [], "interval": "2h", "nowMs": NOW})
        self.verify(operations)

    def test_wilder_rsi_and_python_rounding_parity(self):
        reference = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
        operations = [{"op": "rsi", "values": reference}, {"op": "rsi", "values": [10] * 30},
                      {"op": "rsi", "values": [1, 2, 3]}, {"op": "ema", "values": reference, "period": 21},
                      {"op": "wilder", "values": reference, "period": 14}]
        values = [0.5, 1.5, 2.5, 3.5, -0.5, -1.5, -2.5, 2.675, 1.005, 0.15, 0.25, -2.675, 9.995,
                  math.nextafter(250.5, math.inf), math.nextafter(250.5, -math.inf)]
        generator = random.Random(77)
        values += [generator.uniform(-1e6, 1e6) for _ in range(60)]
        for value in values:
            for digits in (-2, -1, 0, 1, 2, 3, 4):
                operations.append({"op": "round", "value": value, "digits": digits})
        self.verify(operations)


if __name__ == "__main__":
    unittest.main(verbosity=2)
