"""Telegram tests use MockTransport only; no message is sent to Telegram."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest

from app.bot import BotService
from app.telegram import TelegramNotifier

TOKEN = '123456789:' + 'test-token-not-real-secret-123456'
CHAT = '-1001234567890'


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', TOKEN)
    monkeypatch.setenv('TELEGRAM_CHAT_ID', CHAT)
    monkeypatch.setenv('BINANCE_API_SECRET', 'binance-test-secret')
    monkeypatch.setenv('APP_TOKEN', 'app-test-token-never-notify')


@pytest.mark.asyncio
async def test_missing_or_invalid_configuration_never_starts_network(monkeypatch):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    notifier = TelegramNotifier()
    await notifier.start()
    assert not notifier.status()['configured']
    assert not notifier.publish('entry', 'BTCUSDT', 'live')
    assert notifier._client is None
    await notifier.stop()
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', TOKEN + '/other-route')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'another-recipient')
    notifier = TelegramNotifier()
    await notifier.start()
    assert not notifier.status()['running']
    assert notifier.last_error and TOKEN not in notifier.last_error
    assert notifier._client is None


@pytest.mark.asyncio
async def test_only_important_events_go_to_exact_configured_chat_and_mode(telegram_env):
    calls = []
    def send(request):
        import json
        calls.append((request, json.loads(request.content)))
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        notifier = TelegramNotifier(client=client, min_interval=0)
        await notifier.start()
        assert not notifier.publish('config', 'routine', 'live')
        assert not notifier.publish('system', 'routine', 'live')
        assert notifier.publish('entry', 'BTCUSDT: الحماية مؤكدة', 'live')
        await notifier._queue.join()
        await notifier.stop()
    assert len(calls) == 1
    request, payload = calls[0]
    assert str(request.url) == f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    assert payload['chat_id'] == CHAT
    assert 'تداول حقيقي' in payload['text'] and 'BTCUSDT' in payload['text']
    assert 'UTC' in payload['text']
    assert 'parse_mode' not in payload


@pytest.mark.asyncio
async def test_errors_and_httpx_logs_never_expose_tokens_or_urls(telegram_env, caplog):
    def reject(request):
        return httpx.Response(403, json={'ok': False, 'description': TOKEN + ' ' + str(request.url)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        notifier = TelegramNotifier(client=client, min_interval=0)
        await notifier.start()
        with caplog.at_level(logging.INFO, logger='httpx'):
            notifier.publish('error', f'failure {TOKEN} binance-test-secret app-test-token-never-notify https://api.telegram.org/bot{TOKEN}/x', 'testnet')
            queued = notifier._queue._queue[0]
            assert TOKEN not in queued['text'] and 'binance-test-secret' not in queued['text']
            assert 'app-test-token-never-notify' not in queued['text']
            assert 'https://' not in queued['text']
            await notifier._queue.join()
        status = notifier.status()
        await notifier.stop()
    assert status['failed'] == 1 and '403' in status['error']
    assert TOKEN not in caplog.text
    assert 'sendMessage' not in caplog.text
    assert TOKEN not in str(status) and CHAT not in str(status)


@pytest.mark.asyncio
async def test_transport_timeout_has_bounded_retry_and_generic_error(telegram_env):
    calls = 0
    def timeout(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f'secret URL {request.url}', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        notifier = TelegramNotifier(client=client, min_interval=0, retry_delay=0)
        await notifier.start()
        notifier.publish('close', 'BTCUSDT مغلق', 'paper')
        await notifier._queue.join()
        assert calls == 2
        assert notifier.failed == 1 and notifier.delivered == 0
        assert TOKEN not in notifier.last_error and 'https://' not in notifier.last_error
        await notifier.stop()


@pytest.mark.asyncio
async def test_transient_server_failure_retries_once_and_chat_failure_does_not(telegram_env):
    statuses = [503, 200, 400]
    calls = []
    def send(request):
        code = statuses.pop(0)
        calls.append(code)
        return httpx.Response(code, json={'ok': code == 200})
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        notifier = TelegramNotifier(client=client, min_interval=0, retry_delay=0)
        await notifier.start()
        notifier.publish('entry', 'BTCUSDT', 'paper')
        await notifier._queue.join()
        notifier.publish('close', 'BTCUSDT', 'paper')
        await notifier._queue.join()
        assert calls == [503, 200, 400]
        assert notifier.delivered == 1 and notifier.failed == 1
        await notifier.stop()


@pytest.mark.asyncio
async def test_queue_is_bounded_nonblocking_and_repeated_errors_deduplicated(telegram_env):
    release = asyncio.Event()
    async def slow(request):
        await release.wait()
        return httpx.Response(200, json={'ok': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        notifier = TelegramNotifier(client=client, queue_size=2, min_interval=0)
        await notifier.start()
        start = time.monotonic()
        assert notifier.publish('error', 'offline', 'live')
        assert not notifier.publish('error', 'offline', 'live')
        for i in range(20):
            notifier.publish('close', f'close {i}', 'live')
        assert time.monotonic() - start < .1
        assert notifier._queue.qsize() == 2
        assert notifier.dropped == 19
        assert 'close 19' in notifier._queue._queue[-1]['text']
        release.set()
        await notifier._queue.join()
        await notifier.stop()


@pytest.mark.asyncio
async def test_rate_limit_applies_to_all_delivery_attempts(telegram_env):
    times = []
    def send(request):
        times.append(time.monotonic())
        return httpx.Response(200, json={'ok': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        notifier = TelegramNotifier(client=client, min_interval=.02)
        await notifier.start()
        notifier.publish('entry', 'entry', 'testnet')
        notifier.publish('close', 'close', 'testnet')
        await notifier._queue.join()
        await notifier.stop()
    assert len(times) == 2 and times[1] - times[0] >= .018


@pytest.mark.asyncio
async def test_shutdown_cancels_slow_delivery_and_cannot_accept_new_alerts(telegram_env):
    started = asyncio.Event()
    async def hang(request):
        started.set()
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(hang)) as client:
        notifier = TelegramNotifier(client=client, min_interval=0)
        await notifier.start()
        await notifier.start()  # No second worker.
        notifier.publish('error', 'urgent', 'live')
        await started.wait()
        await asyncio.wait_for(notifier.stop(flush_timeout=.01), .2)
        assert not notifier.status()['running']
        assert notifier.dropped == 1
        assert not notifier.publish('close', 'after shutdown', 'live')
        assert not client.is_closed  # Caller owns the injected client.
        await notifier.start()
        await notifier.stop(flush_timeout=0)


@pytest.mark.asyncio
async def test_bot_notification_failure_never_changes_trade_state(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DB_PATH', str(tmp_path / 'bot.sqlite3'))
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    bot = BotService(object())
    class BrokenNotifier:
        def publish(self, *args): raise RuntimeError('notification failure')
        def status(self): return {'configured': False}
    bot.notifications = BrokenNotifier()
    bot.state = 'running'
    bot._event('entry', 'BTCUSDT: confirmed protection')
    assert bot.state == 'running'
    assert bot.ledger.events()[0]['type'] == 'entry'
    bot._fail(ValueError('domain error'))
    assert bot.state == 'error'  # Only trading error changes trading state.
    assert bot.ledger.events()[0]['type'] == 'error'


@pytest.mark.asyncio
async def test_telegram_long_429_cooldown_is_honored_without_early_retry(telegram_env):
    calls = []
    def limited(request):
        calls.append(request)
        return httpx.Response(429, json={'ok': False, 'parameters': {'retry_after': 120}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        notifier = TelegramNotifier(client=client, min_interval=0, retry_delay=0)
        await notifier.start()
        notifier.publish('entry', 'BTCUSDT', 'live')
        await notifier._queue.join()
        assert len(calls) == 1 and notifier.failed == 1
        assert notifier._cooldown_until > time.monotonic() + 110
        notifier.publish('close', 'BTCUSDT', 'live')
        await asyncio.sleep(.01)
        assert len(calls) == 1  # The next queued alert does not evade Telegram's cooldown.
        await asyncio.wait_for(notifier.stop(flush_timeout=.01), .2)
