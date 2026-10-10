"""Best-effort Telegram notifications. Never a trading command channel.

Secrets and the sole recipient come from server environment variables. The
bounded worker is separate from exchange execution: Telegram cannot delay a
stop order, alter a position, or arm the bot. Pending notifications are not a
replacement for the durable trading ledger.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import suppress
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

IMPORTANT_EVENTS = frozenset({'entry', 'close', 'error', 'circuit', 'pause', 'armed'})
MODE_LABELS = {'paper': 'محاكاة — بلا أموال حقيقية', 'testnet': 'اختبار Testnet', 'live': 'تداول حقيقي'}


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    result = str(text)
    for secret in secrets:
        if secret:
            result = result.replace(secret, '[محجوب]').replace(quote(secret, safe=''), '[محجوب]')
    # Token-bearing Telegram URLs must never enter logs, delivery errors or alerts.
    result = re.sub(r'https?://[^\s<>]+', '[عنوان محجوب]', result)
    return re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot[محجوب]', result)


class _SecretFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...]):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message, self.secrets)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True


class TelegramNotifier:
    def __init__(
        self, *, client: httpx.AsyncClient | None = None, queue_size: int = 64,
        min_interval: float = 3.0, retry_delay: float = 0.5,
    ):
        self._token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        self._chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        self.configured = bool(re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', self._token)
                               and re.fullmatch(r'-?[1-9]\d{0,19}', self._chat))
        self._secrets = tuple(filter(None, (self._token, os.getenv('BINANCE_API_KEY', ''),
                                          os.getenv('BINANCE_API_SECRET', ''), os.getenv('APP_TOKEN', ''))))
        self._client = client
        self._owns_client = client is None
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max(1, min(256, queue_size)))
        self._min_interval = max(0.0, min_interval)
        self._retry_delay = max(0.0, retry_delay)
        self._task: asyncio.Task | None = None
        self._accepting = False
        self._last_attempt = 0.0
        self._cooldown_until = 0.0
        self._dedupe: dict[tuple[str, str, str], float] = {}
        self._filter = _SecretFilter(self._secrets)
        self.delivered = 0
        self.failed = 0
        self.dropped = 0
        self.last_error: str | None = 'إعداد تيليغرام غير مكتمل أو غير صالح' if (self._token or self._chat) and not self.configured else None

    async def start(self) -> None:
        if not self.configured or (self._task and not self._task.done()):
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4), follow_redirects=False)
        logging.getLogger('httpx').addFilter(self._filter)
        self._accepting = True
        self._task = asyncio.create_task(self._run(), name='nabd-telegram-notifications')

    async def stop(self, flush_timeout: float = 2.0) -> None:
        self._accepting = False
        task = self._task
        if task:
            # Bounded grace period; trading shutdown never waits indefinitely for Telegram.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout=max(0.0, min(flush_timeout, 5.0)))
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._task = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            self.dropped += 1
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        logging.getLogger('httpx').removeFilter(self._filter)

    def publish(self, kind: str, message: str, mode: str) -> bool:
        """Nonblocking: only designated bot events, only the preconfigured chat."""
        if not self.configured or not self._accepting or kind not in IMPORTANT_EVENTS:
            return False
        safe_message = redact(message, self._secrets)[:3000]
        stamp = time.monotonic()
        if kind in ('error', 'circuit', 'pause'):
            key = (kind, mode, safe_message)
            if stamp - self._dedupe.get(key, float('-inf')) < 300:
                return False
            self._dedupe = {k: at for k, at in self._dedupe.items() if stamp - at < 300}
            self._dedupe[key] = stamp
        utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        payload = {'chat_id': self._chat,
                   'text': f"نبض | {MODE_LABELS.get(mode, 'وضع غير معروف')}\n{safe_message}\n{utc}",
                   'disable_web_page_preview': True}
        if self._queue.full():
            # Preserve the most recent alert; never block the trading transaction.
            self._queue.get_nowait()
            self._queue.task_done()
            self.dropped += 1
        self._queue.put_nowait(payload)
        return True

    def status(self) -> dict:
        return dict(configured=self.configured, running=bool(self._task and not self._task.done()),
                    queued=self._queue.qsize(), delivered=self.delivered, failed=self.failed,
                    dropped=self.dropped, error=self.last_error)

    async def _run(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                sent = await self._deliver(payload)
                if sent:
                    self.delivered += 1
                    self.last_error = None
                else:
                    self.failed += 1
            except asyncio.CancelledError:
                self.dropped += 1
                raise
            except Exception:
                # Do not log request exceptions: their URL contains the bot token.
                self.failed += 1
                self.last_error = 'تعذر إرسال إشعار تيليغرام؛ التداول وأوامر الحماية مستقلان'
            finally:
                self._queue.task_done()

    async def _deliver(self, payload: dict) -> bool:
        for attempt in range(2):
            stamp = time.monotonic()
            delay = max(self._min_interval - (stamp - self._last_attempt), self._cooldown_until - stamp)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_attempt = time.monotonic()
            retry_wait = self._retry_delay
            try:
                response = await self._client.post(f'https://api.telegram.org/bot{self._token}/sendMessage', json=payload)
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if response.status_code == 200 and isinstance(data, dict) and data.get('ok') is True:
                    return True
                self.last_error = f'تيليغرام رفض الإشعار (HTTP {response.status_code})'
                if response.status_code == 429:
                    try:
                        retry_wait = max(self._retry_delay, float(data.get('parameters', {}).get('retry_after', 1)))
                        if not 0 <= retry_wait < float('inf'):
                            retry_wait = 1.0
                    except (ValueError, TypeError, AttributeError):
                        retry_wait = max(1.0, self._retry_delay)
                    self._cooldown_until = time.monotonic() + retry_wait
                    if retry_wait > 30:
                        return False  # Drop this alert; queued alerts honor the server's full cooldown.
                elif response.status_code < 500:
                    return False  # Authentication / chat errors require configuration, not repeated requests.
            except httpx.HTTPError:
                self.last_error = 'تعذر الاتصال بتيليغرام؛ نتيجة تسليم الإشعار غير مؤكدة'
            if attempt == 0:
                await asyncio.sleep(retry_wait)
        return False
