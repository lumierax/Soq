"""Public market API and authenticated futures control plane."""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
import re
import secrets
import time
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .analysis import AnalysisUnavailable, REQUIRED_TIMEFRAMES
from .backtest import run_backtest
from .bot import BotService
from .market import MarketService, MarketUnavailable


Market = Literal["spot", "futures"]
Horizon = Literal["swing", "investment", "intraday"]
market_service = MarketService()
bot_service = BotService(market_service)
started_at = int(time.time() * 1000)
backtest_lock = asyncio.Lock()
backtest_cache: dict[tuple, tuple[float, dict]] = {}
auth_scheme = HTTPBearer(auto_error=False)


def load_token() -> str:
    configured = os.getenv("APP_TOKEN", "").strip()
    if configured:
        if len(configured) < 24 or configured == "replace-with-a-long-random-token":
            raise RuntimeError("APP_TOKEN must be a unique random token of at least 24 characters")
        return configured
    directory = Path(os.getenv("NABD_DATA_DIR", "./data"))
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    token_path = directory / ".app-token"
    try:
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token_path.chmod(0o600)
        value = token_path.read_text().strip()
        if len(value) < 24:
            raise RuntimeError("Invalid local .app-token: replace with a random APP_TOKEN")
        return value
    value = secrets.token_urlsafe(36)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
    return value


APP_TOKEN = load_token()


def require_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(auth_scheme)]) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(credentials.credentials, APP_TOKEN):
        raise HTTPException(401, "أدخل رمز الوصول إلى الخادم في الإعدادات", headers={"WWW-Authenticate": "Bearer"})


def validate_selection(market: str, horizon: str, symbol: str | None = None) -> None:
    if (market, horizon) not in REQUIRED_TIMEFRAMES:
        raise HTTPException(422, "اختر سوينج أو استثمار للسبوت، ولحظي للفيوتشر")
    if symbol is not None and not valid_symbol(symbol):
        raise HTTPException(422, "زوج USDT غير صالح")


def valid_symbol(symbol: str) -> bool:
    # Binance also lists one-letter and Unicode base assets. Preserve exact
    # exchange symbols; transport layers percent-encode query parameters.
    return symbol.endswith("USDT") and 1 <= len(symbol[:-4]) <= 40 and symbol[:-4].isalnum()


def safe_error(error: Exception) -> str:
    # Only expose our short domain errors. HTTP/signed URLs and exception
    # representations can contain key headers, signatures or account data.
    if isinstance(error, (ValueError, AnalysisUnavailable, MarketUnavailable)):
        message = str(error)
        if len(message) <= 350 and not any(word in message.lower() for word in ("signature=", "api_key", "secret", "https://")):
            return message
    return "تعذر إتمام الطلب. تحقق من اتصال الخادم وبيانات Binance ثم أعد المحاولة."


@asynccontextmanager
async def lifespan(_: FastAPI):
    await market_service.start()
    await bot_service.start()
    try:
        yield
    finally:
        await bot_service.stop()
        await market_service.stop()


app = FastAPI(title="NABD", version="1.1.1", lifespan=lifespan)
origins = os.getenv("CORS_ORIGINS", "capacitor://localhost,http://localhost,http://localhost:5173,http://127.0.0.1:5173").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[origin.strip() for origin in origins if origin.strip()],
                   allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/bot"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unhandled(_: Request, error: Exception):
    return JSONResponse(status_code=503, content={"detail": safe_error(error)})


@app.get("/api/health")
async def health():
    return {"service": "nabd", "version": "1.0.0", "time": int(time.time() * 1000),
            "startedAt": started_at, "market": market_service.status()}


@app.get("/api/markets")
async def markets(market: Market = "spot", horizon: Horizon = "swing"):
    validate_selection(market, horizon)
    return await market_service.snapshot(market, horizon)


@app.get("/api/candles")
async def candles(market: Market = "spot", symbol: str = "BTCUSDT", interval: str = "4h", limit: int = 500):
    if not valid_symbol(symbol):
        raise HTTPException(422, "زوج USDT غير صالح")
    permitted = {"spot": {"4h", "1d", "1w"}, "futures": {"3m", "5m", "15m", "30m", "1h"}}
    if interval not in permitted[market] or not 100 <= limit <= 1000:
        raise HTTPException(422, "إطار زمني أو عدد شموع غير مدعوم")
    try:
        return await market_service.candles(market, symbol, interval, limit)
    except (ValueError, AnalysisUnavailable, MarketUnavailable) as error:
        raise HTTPException(503, safe_error(error)) from None


@app.get("/api/analysis")
async def analysis(market: Market = "spot", symbol: str = "BTCUSDT", horizon: Horizon = "swing"):
    validate_selection(market, horizon, symbol)
    try:
        return await market_service.analyze_symbol(market, symbol, horizon)
    except (ValueError, AnalysisUnavailable, MarketUnavailable) as error:
        raise HTTPException(503, safe_error(error)) from None


@app.get("/api/backtest")
async def backtest(market: Market = "spot", symbol: str = "BTCUSDT", horizon: Horizon = "swing"):
    validate_selection(market, horizon, symbol)
    key = (market, symbol, horizon)
    cached = backtest_cache.get(key)
    if cached and time.monotonic() - cached[0] < 900:
        return cached[1]
    if backtest_lock.locked():
        raise HTTPException(429, "اختبار آخر يعمل الآن؛ أعد المحاولة بعد قليل", headers={"Retry-After": "30"})
    async with backtest_lock:
        try:
            result = await asyncio.wait_for(run_backtest(market_service, market, symbol, horizon), timeout=100)
            if len(backtest_cache) >= 64:
                backtest_cache.pop(next(iter(backtest_cache)))
            backtest_cache[key] = (time.monotonic(), result)
            return result
        except (ValueError, AnalysisUnavailable, MarketUnavailable, TimeoutError) as error:
            raise HTTPException(503, safe_error(error)) from None


@app.get("/api/research")
async def research():
    return {
        "strategyVersion": "1.0", "scoreMeaning": "قوة إعداد فني من 100، وليست احتمال ربح",
        "inputs": ["شموع مغلقة", "اتجاه متعدد الأطر", "RSI وMACD وADX", "الحجم والسيولة",
                   "ATR وبنية الدعم والمقاومة", "سياق بيتكوين", "تمويل الفيوتشر"],
        "excluded": ["تحليل الأخبار والأحداث", "تقييم مشاريع العملات وفرقها", "تحليل السلسلة وتدفقات المحافظ",
                     "دفتر أوامر تاريخي كامل", "تنبؤ مضمون أو نسبة نجاح مثبتة"],
        "validation": "تحتاج الاستراتيجية إلى اختبار تاريخي مستقل وتشغيل تجريبي قبل الاعتماد عليها بأموال حقيقية.",
    }


class BotConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    riskPercent: float = Field(default=1, gt=0, le=3)
    maxPositions: int = Field(default=3, ge=1, le=10)
    maxDailyLossPercent: float = Field(default=4, gt=0, le=15)
    leverage: int = Field(default=3, ge=1, le=10)
    minScore: int = Field(default=75, ge=70, le=100)
    minRiskReward: float = Field(default=2, ge=1.8, le=10)
    capital: float = Field(default=1000, ge=50, le=1_000_000)


class ArmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["paper", "testnet", "live"] = "paper"
    confirmation: str = ""


bot_auth = [Depends(require_token)]


@app.get("/api/bot", dependencies=bot_auth)
async def bot_status():
    return await bot_service.status()


@app.get("/api/bot/history", dependencies=bot_auth)
async def bot_history():
    return await bot_service.history()


@app.post("/api/bot/config", dependencies=bot_auth)
async def bot_config(config: BotConfigInput):
    try:
        return await bot_service.configure(config.model_dump())
    except ValueError as error:
        raise HTTPException(409, safe_error(error)) from None


@app.post("/api/bot/arm", dependencies=bot_auth)
async def bot_arm(command: ArmInput):
    try:
        return await bot_service.arm(command.mode, command.confirmation)
    except ValueError as error:
        raise HTTPException(409, safe_error(error)) from None


@app.post("/api/bot/pause", dependencies=bot_auth)
async def bot_pause():
    return await bot_service.pause()


@app.post("/api/bot/close/{position_id}", dependencies=bot_auth)
async def bot_close(position_id: str):
    if not re.fullmatch(r"[a-zA-Z0-9_\-]{1,80}", position_id):
        raise HTTPException(422, "معرف مركز غير صالح")
    try:
        return await bot_service.close_position(position_id)
    except ValueError as error:
        raise HTTPException(409, safe_error(error)) from None


@app.api_route("/api/{unknown:path}", methods=["GET", "POST"])
async def api_not_found(unknown: str):
    raise HTTPException(404, "مسار API غير موجود")


dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
# Backend lives backend/app, so parents[2] is the project root.
if dist.is_dir():
    class SPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            try:
                return await super().get_response(path, scope)
            except Exception as error:
                if getattr(error, "status_code", None) == 404 and "." not in path.rsplit("/", 1)[-1]:
                    return FileResponse(dist / "index.html")
                raise
    app.mount("/", SPAStaticFiles(directory=dist, html=True), name="frontend")
