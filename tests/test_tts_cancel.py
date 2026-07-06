"""Unit tests for Deepgram TTS cancel_event barge-in behavior."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from backend.tts import DeepgramTTSEngine


def _make_mock_ws(messages: list) -> MagicMock:
    class MockWebSocket:
        def __init__(self):
            self.closed = False
            self.send_json = AsyncMock()
            self._messages = messages

        def __aiter__(self):
            async def iterator():
                for msg in self._messages:
                    await asyncio.sleep(0.01)
                    yield msg
            return iterator()

    return MockWebSocket()


@pytest.mark.asyncio
async def test_cancel_event_sends_clear_and_preserves_ws():
    """cancel_event barge-in must send Clear (not Flush) and keep the persistent WS."""
    engine = DeepgramTTSEngine(api_key="fake", model="fake")
    cancel_event = asyncio.Event()

    messages = [
        SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"audio1"),
        SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"audio2"),
    ]
    mock_ws = _make_mock_ws(messages)

    with patch("aiohttp.ClientSession") as mock_session_class:
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        mock_session_class.return_value = mock_session

        engine._ws = mock_ws

        chunk_queue: asyncio.Queue = asyncio.Queue()
        await chunk_queue.put("Hello world")
        # No sentinel — cancel mid-stream before feeder can Flush.

        async def run():
            chunks = []
            async for chunk in engine.synthesise_streaming(
                chunk_queue, timeout_sec=5, cancel_event=cancel_event
            ):
                chunks.append(chunk)
                if len(chunks) == 1:
                    cancel_event.set()
            return chunks

        await run()

    sent_types = [call.args[0]["type"] for call in mock_ws.send_json.call_args_list]
    assert "Clear" in sent_types
    assert "Flush" not in sent_types
    assert engine._ws is mock_ws


@pytest.mark.asyncio
async def test_unclean_shutdown_reconnects():
    """A stream that ends without Flushed and without cancel must tear down the WS."""
    engine = DeepgramTTSEngine(api_key="fake", model="fake")

    messages = [
        SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"audio1"),
        SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None),
    ]
    mock_ws = _make_mock_ws(messages)

    with patch.object(engine, "_clear_and_close_ws", new_callable=AsyncMock) as mock_close:
        engine._ws = mock_ws

        chunk_queue: asyncio.Queue = asyncio.Queue()
        await chunk_queue.put("Hello")
        await chunk_queue.put(None)

        async for _ in engine.synthesise_streaming(chunk_queue, timeout_sec=5):
            pass

    mock_close.assert_awaited_once()
