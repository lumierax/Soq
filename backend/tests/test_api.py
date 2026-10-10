"""Authentication and route contracts; no exchange orders or external calls."""
from unittest.mock import patch
import importlib
import os
import tempfile

import httpx
import pytest

TEST_TOKEN = "nabd-test-access-token-never-used-in-production-0000"


@pytest.fixture(scope="module")
def api_module():
    with tempfile.TemporaryDirectory() as directory:
        with patch.dict(os.environ, {"APP_TOKEN": TEST_TOKEN, "BOT_DB_PATH": directory + "/test.db", "NABD_DATA_DIR": directory}):
            module = importlib.import_module("app.main")
        yield module


class FakeMarket:
    def status(self):
        return {"running": True}

    async def snapshot(self, market, horizon):
        return {"market": market, "horizon": horizon, "items": [], "connection": "offline"}

    async def candles(self, market, symbol, interval, limit):
        return [{"symbol": symbol, "interval": interval, "limit": limit}]

    async def analyze_symbol(self, market, symbol, horizon):
        return {"symbol": symbol, "market": market, "horizon": horizon}


class FakeBot:
    calls = []

    async def status(self):
        return {"state": "stopped", "mode": "paper"}

    async def configure(self, config):
        self.calls.append(("configure", config))
        return {"config": config}

    async def arm(self, mode, confirmation):
        self.calls.append(("arm", mode, confirmation))
        return {"mode": mode}

    async def pause(self):
        self.calls.append(("pause",))
        return {"state": "paused"}

    async def close_position(self, position):
        self.calls.append(("close", position))
        return {"closed": position}

    async def history(self):
        return []


@pytest.fixture
async def client(api_module, monkeypatch):
    bot = FakeBot()
    bot.calls = []
    monkeypatch.setattr(api_module, "market_service", FakeMarket())
    monkeypatch.setattr(api_module, "bot_service", bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app, raise_app_exceptions=False), base_url="http://test") as client:
        yield client, bot


async def test_every_bot_route_requires_token(client):
    http, bot = client
    for method, path, body in [
        ("GET", "/api/bot", None), ("GET", "/api/bot/history", None),
        ("POST", "/api/bot/config", {}), ("POST", "/api/bot/arm", {"mode": "live"}),
        ("POST", "/api/bot/pause", None), ("POST", "/api/bot/close/123", None),
    ]:
        response = await http.request(method, path, json=body)
        assert response.status_code == 401
        assert "no-store" in response.headers["cache-control"]
    assert bot.calls == []


async def test_wrong_token_cannot_arm(client):
    http, bot = client
    response = await http.post("/api/bot/arm", json={"mode": "live", "confirmation": "ENABLE LIVE TRADING"},
                               headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401 and bot.calls == []


async def test_authenticated_commands_and_configuration_bounds(client):
    http, bot = client
    headers = {"Authorization": "Bearer " + TEST_TOKEN}
    invalid = await http.post("/api/bot/config", headers=headers, json={"riskPercent": 4})
    assert invalid.status_code == 422 and bot.calls == []
    extra = await http.post("/api/bot/config", headers=headers, json={"apiSecret": "not-a-secret"})
    assert extra.status_code == 422 and bot.calls == []
    response = await http.post("/api/bot/arm", headers=headers, json={"mode": "live", "confirmation": "ENABLE LIVE TRADING"})
    assert response.status_code == 200
    assert bot.calls == [("arm", "live", "ENABLE LIVE TRADING")]


async def test_unicode_and_single_letter_binance_pairs_work(client):
    http, _ = client
    for symbol in ("币安人生USDT", "TUSDT", "BTCUSDT"):
        response = await http.get("/api/candles", params={"symbol": symbol})
        assert response.status_code == 200 and response.json()[0]["symbol"] == symbol
        response = await http.get("/api/analysis", params={"symbol": symbol})
        assert response.status_code == 200
    for symbol in ("../../USDT", "BTC/USDT", "USDT", "BTCUSDT?secret=1"):
        response = await http.get("/api/candles", params={"symbol": symbol})
        assert response.status_code == 422


async def test_separate_horizons_and_intervals(client):
    http, _ = client
    for params in ({"market": "spot", "horizon": "intraday"}, {"market": "futures", "horizon": "swing"}):
        assert (await http.get("/api/markets", params=params)).status_code == 422
    assert (await http.get("/api/candles", params={"market": "spot", "interval": "3m"})).status_code == 422
    assert (await http.get("/api/candles", params={"market": "futures", "interval": "1w"})).status_code == 422
    assert (await http.get("/api/markets")).status_code == 200


async def test_cors_for_native_origin_and_unknown_api(client):
    http, _ = client
    response = await http.options("/api/bot", headers={"Origin": "capacitor://localhost", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "capacitor://localhost"
    assert (await http.get("/api/not-real")).status_code == 404
