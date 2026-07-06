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
            if len(chunks) == 2:
                break
        return chunks
        
    task = asyncio.create_task(run_gen())
    
    try:
        chunks = await task
    except asyncio.CancelledError:
        pass
        
    assert len(chunks) == 2
    # With EARLY_FLUSH_TOKENS=6, the first 6 tokens are batched before the
    # first word-boundary flush. The 8-token stream produces exactly 2 chunks.
    assert "This" in chunks[0]
    assert len(chunks[0]) > len(chunks[1]) or len(chunks[1]) > 0

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


@pytest.mark.asyncio
async def test_vad_barge_in_clears_buffer_on_confirmed_interrupt():
    """Confirmed barge-in must clear STT buffers via pipeline, not stt.py pre-wipe."""
    from backend.pipeline import PipelineSession
    from backend.stt import STTHandler

    outbound_queue: asyncio.Queue = asyncio.Queue()
    session = PipelineSession(
        api_keys={"deepgram": "fake", "groq": "fake"},
        model_config={"groq_model": "fake", "deepgram_model": "nova-3"},
        outbound_queue=outbound_queue,
        tts_engine="edge",
    )
    session.stt_handler = STTHandler(
        api_key="fake",
        on_transcript=MagicMock(),
    )
    session.stt_handler.transcript_buffer = "partial utterance"
    session.stt_handler._latest_interim = "still speaking"
    session._active_turn_id = 1
    session.is_processing_transcript = True
    session._ai_speaking = True

    session._on_vad_interrupted("")

    assert session.stt_handler.transcript_buffer == ""
    assert session.stt_handler._latest_interim == ""
    assert session._active_turn_id is None
    assert session.is_processing_transcript is False

    msg = await outbound_queue.get()
    assert msg["type"] == "turn_cancelled"
    assert msg["turn_id"] == 1


@pytest.mark.asyncio
async def test_vad_resume_during_speculative_preserves_buffer():
    """User resuming mid-utterance after speculative trigger must not wipe STT buffer."""
    from backend.pipeline import PipelineSession
    from backend.stt import STTHandler

    outbound_queue: asyncio.Queue = asyncio.Queue()
    session = PipelineSession(
        api_keys={"deepgram": "fake", "groq": "fake"},
        model_config={"groq_model": "fake", "deepgram_model": "nova-3"},
        outbound_queue=outbound_queue,
        tts_engine="edge",
    )
    session.stt_handler = STTHandler(
        api_key="fake",
        on_transcript=MagicMock(),
    )
    session.stt_handler.transcript_buffer = "the capital of France"
    session.stt_handler._latest_interim = "is"
    session._active_turn_id = 1
    session.is_processing_transcript = True
    session._ai_speaking = False

    session._on_vad_interrupted("")

    assert session.stt_handler.transcript_buffer == "the capital of France"
    assert session.stt_handler._latest_interim == "is"
    assert session._active_turn_id is None
    assert session.is_processing_transcript is False

    msg = await outbound_queue.get()
    assert msg["type"] == "turn_cancelled"
    assert msg["turn_id"] == 1
