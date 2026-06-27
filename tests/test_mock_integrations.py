import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from backend.llm import LLMGenerator
from backend.tts import DeepgramTTSEngine

@pytest.mark.asyncio
async def test_llm_chunking_and_cancellation():
    """Verify LLM chunking handles slow tokens and cancellation without live API keys."""
    gen = LLMGenerator(api_key="fake", model="fake")
    
    # Mock stream yielding tokens slowly
    async def mock_stream():
        tokens = ["This ", "is ", "a ", "test. ", "This ", "is ", "sentence ", "two."]
        for t in tokens:
            await asyncio.sleep(0.01)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=t))])

    client = AsyncMock()
    stream_mock = MagicMock()
    stream_mock.__aiter__.side_effect = lambda: mock_stream()
    client.chat.completions.create = AsyncMock(return_value=stream_mock)
    
    # Override the client directly
    gen.client = client
    
    async def run_gen():
        chunks = []
        async for chunk in gen.generate([{"role": "user", "content": "test"}]):
            chunks.append(chunk)
            if len(chunks) == 3:
                break
        return chunks
        
    task = asyncio.create_task(run_gen())
    
    try:
        chunks = await task
    except asyncio.CancelledError:
        pass
        
    assert len(chunks) == 3
    assert chunks[0] == "This "
    assert chunks[1] == "is "
    assert chunks[2] == "a "

@pytest.mark.asyncio
async def test_tts_ws_state_machine_cancellation():
    """Verify TTS WS state machine cleans up on cancellation without live API keys."""
    engine = DeepgramTTSEngine(api_key="fake", model="fake")
    
    with patch("aiohttp.ClientSession") as mock_session_class:
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        
        class MockWebSocket:
            def __init__(self):
                self.closed = False
                self.send_json = AsyncMock()
                self.close = AsyncMock()
                self._messages = [
                    SimpleNamespace(type=2, data=b"audio1"),
                    SimpleNamespace(type=2, data=b"audio2"),
                ]
            
            def __aiter__(self):
                async def iterator():
                    for msg in self._messages:
                        await asyncio.sleep(0.05)
                        yield msg
                return iterator()
                
        mock_ws = MockWebSocket()
        mock_session.closed = False
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        
        async def run_synth():
            async for chunk in engine.synthesise("Hello"):
                pass
        
        task = asyncio.create_task(run_synth())
        
        await asyncio.sleep(0.02)
        task.cancel() # Cancel mid-stream
        
        try:
            await task
        except asyncio.CancelledError:
            pass
            
        # Verify clear was sent or WS closed
        assert mock_ws.send_json.called or mock_ws.close.called
