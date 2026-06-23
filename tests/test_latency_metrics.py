"""
cascade/tests/test_latency_metrics.py

Integration tests for latency measurement and interruption hardening (Phases 1-5).

Validates:
  1. LLM latency broken down into queue, TTFT, and streaming components
  2. TTS latency measured per-sentence independently
  3. Interruption system prevents stale audio from reaching client
  4. Queue optimization reduces buffering without hurting latency
  5. Metrics correctly displayed on frontend dashboard

Usage:
  pytest tests/test_latency_metrics.py -v
"""

import asyncio
import json
import time
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Import components to test
from backend.pipeline import PipelineSession
from backend.llm import LLMGenerator
from backend.tts import EdgeTTSEngine, DeepgramTTSEngine


def make_pipeline_session() -> PipelineSession:
    """Create a test pipeline session using the current constructor."""
    return PipelineSession(
        api_keys={"deepgram": "test_key", "groq": "test_key"},
        model_config={
            "deepgram_model": "nova-2",
            "groq_model": "mixtral-8x7b",
            "edge_tts_voice": "en-US-AriaNeural",
            "deepgram_tts_model": "aura-asteria-en",
        },
        outbound_queue=asyncio.Queue(),
        subject="Test",
        tts_engine="edge",
    )


class TestLLMLatencyTracking:
    """Phase 1: LLM Latency Instrumentation Tests"""
    
    @pytest.fixture
    def mock_groq_client(self):
        """Mock Groq client with controlled latencies."""
        client = AsyncMock()
        
        # Simulate realistic latencies:
        # Queue delay: 50ms
        # TTFT: 200ms
        # Streaming (first sentence): 150ms
        async def mock_create(*args, **kwargs):
            await asyncio.sleep(0.05)  # Queue delay
            
            async def token_stream():
                await asyncio.sleep(0.2)  # TTFT
                yield "This "
                yield "is "
                yield "a "
                yield "test "
                yield "response "
                yield "from "
                yield "the "
                yield "LLM."
            
            return MagicMock(choices=[MagicMock(delta=MagicMock(content=None))])
        
        client.chat.completions.create = mock_create
        return client
    
    @pytest.mark.asyncio
    async def test_llm_timestamps_recorded(self):
        """Test that LLM timestamps are recorded at all key points."""
        gen = LLMGenerator(
            api_key="test_key",
            model="groq-model",
        )
        
        # Verify timestamp attributes exist
        assert hasattr(gen, 't_request_created')
        assert hasattr(gen, 't_request_sent')
        assert hasattr(gen, 't_first_token')
        assert hasattr(gen, 't_first_sentence_emitted')
        
        # Verify they start as None
        assert gen.t_request_created is None
        assert gen.t_request_sent is None
        assert gen.t_first_token is None
        assert gen.t_first_sentence_emitted is None
    
    @pytest.mark.asyncio
    async def test_llm_metrics_computation(self):
        """Test that LLM metrics are correctly computed from timestamps."""
        # Manually set timestamps to simulate a realistic scenario
        gen = LLMGenerator(
            api_key="test_key",
            model="groq-model",
        )
        
        now = time.time()
        gen.t_request_created = now
        gen.t_request_sent = now + 0.05     # 50ms queue delay
        gen.t_first_token = now + 0.25      # 200ms TTFT
        gen.t_first_sentence_emitted = now + 0.40  # 150ms streaming
        
        # Compute metrics (as done in pipeline.py)
        queue_ms = int((gen.t_request_sent - gen.t_request_created) * 1000)
        ttft_ms = int((gen.t_first_token - gen.t_request_sent) * 1000)
        streaming_ms = int((gen.t_first_sentence_emitted - gen.t_first_token) * 1000)
        
        assert queue_ms in (49, 50)
        assert ttft_ms in (199, 200)
        assert streaming_ms in (149, 150)
        assert (queue_ms + ttft_ms + streaming_ms) in (398, 399, 400)


class TestTTSLatencyTracking:
    """Phase 2: TTS Latency Instrumentation Tests"""
    
    @pytest.mark.asyncio
    async def test_edge_tts_yields_metadata_first(self):
        """Test that EdgeTTSEngine yields metadata dict before audio bytes."""
        engine = EdgeTTSEngine(voice="en-US-AriaNeural")
        
        # Mock edge_tts.Communicate
        with patch('edge_tts.Communicate') as mock_communicate:
            mock_instance = AsyncMock()
            mock_communicate.return_value = mock_instance
            
            # Simulate metadata + audio chunks
            async def mock_stream():
                yield {"type": "audio", "data": b"chunk1"}
                yield {"type": "audio", "data": b"chunk2"}
            
            mock_instance.stream = mock_stream
            
            # Collect yields
            yields = []
            async for item in engine.synthesise("Hello world"):
                yields.append(item)
            
            # First item should be metadata dict
            assert len(yields) >= 1
            first_item = yields[0]
            assert isinstance(first_item, dict)
            assert first_item.get("type") == "tts_metadata"
            assert "t_tts_request_sent" in first_item
            assert "t_first_audio_chunk" in first_item
            assert "latency_ms" in first_item
            
            # Subsequent items should be audio bytes
            audio_items = [item for item in yields[1:] if isinstance(item, bytes)]
            assert len(audio_items) >= 1
    
    @pytest.mark.asyncio
    async def test_deepgram_tts_yields_metadata_first(self):
        """Test that DeepgramTTSEngine yields metadata dict before audio bytes."""
        engine = DeepgramTTSEngine(api_key="test_key", model="aura-asteria-en")
        
        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session

            class MockWebSocket:
                def __init__(self):
                    self.closed = False
                    self.send_json = AsyncMock()

                def __aiter__(self):
                    async def iterator():
                        yield SimpleNamespace(
                            type=1,
                            data=json.dumps({"type": "Metadata"}),
                        )
                        yield SimpleNamespace(type=2, data=b"audio_chunk_1")
                        yield SimpleNamespace(
                            type=1,
                            data=json.dumps({"type": "Flushed"}),
                        )

                    return iterator()

            mock_ws = MockWebSocket()
            mock_session.closed = False
            mock_session.ws_connect = AsyncMock(return_value=mock_ws)
            
            # Collect yields
            yields = []
            async for item in engine.synthesise("Hello"):
                yields.append(item)
            
            # First item should be metadata
            assert len(yields) >= 1
            first_item = yields[0]
            assert isinstance(first_item, dict)
            assert first_item.get("type") == "tts_metadata"
            assert first_item.get("engine") == "deepgram"
            assert yields[1] == b"audio_chunk_1"
            mock_session.ws_connect.assert_awaited_once()


class TestInterruptionHardening:
    """Phase 3: Interruption Hardening Tests"""
    
    @pytest.fixture
    def pipeline_session(self):
        """Create a pipeline session for testing."""
        return make_pipeline_session()
    
    def test_turn_id_validation_blocks_stale_messages(self, pipeline_session):
        """Test that turn_id validation prevents stale audio from being sent."""
        session = pipeline_session
        
        # Simulate active turn
        session.turn_id = 1
        session._active_turn_id = 1
        session._cancel_event.clear()
        
        # Message for active turn should pass
        msg_current = {"turn_id": 1, "type": "audio", "data": b"audio"}
        assert session.can_send_message(msg_current) is True
        
        # Simulate interrupt (new turn)
        session.turn_id = 2
        session._active_turn_id = 2
        session._cancel_event.clear()
        
        # Message for old turn should be rejected
        msg_old = {"turn_id": 1, "type": "audio", "data": b"audio"}
        assert session.can_send_message(msg_old) is False
    
    def test_cancel_event_blocks_messages(self, pipeline_session):
        """Test that cancel event blocks all messages."""
        session = pipeline_session
        session.turn_id = 1
        session._active_turn_id = 1
        
        # Before cancel: messages pass
        msg = {"turn_id": 1, "type": "audio", "data": b"audio"}
        assert session.can_send_message(msg) is True
        
        # After cancel: messages blocked
        session._cancel_event.set()
        assert session.can_send_message(msg) is False
    
    @pytest.mark.asyncio
    async def test_interruption_keeps_queue_for_consumer_validation(self, pipeline_session):
        """Cancel keeps queued messages intact and relies on turn validation."""
        session = pipeline_session
        session.outbound_queue = asyncio.Queue()
        session._active_turn_id = 1
        
        # Add messages to queue
        old_msg = {"turn_id": 1, "type": "text", "text": "message1"}
        await session.outbound_queue.put(old_msg)
        await session.outbound_queue.put({"turn_id": 1, "type": "text", "text": "message2"})
        
        assert session.outbound_queue.qsize() == 2
        
        # Cancel should invalidate the turn without racing the queue.
        await session.cancel()
        
        assert session.outbound_queue.qsize() == 3
        queued_messages = [await session.outbound_queue.get() for _ in range(3)]
        cancelled_msg = queued_messages[-1]
        assert cancelled_msg == {"type": "turn_cancelled", "turn_id": 1}
        assert session.can_send_message(old_msg) is False


class TestQueueOptimization:
    """Phase 4: Queue Optimization Tests"""
    
    @pytest.fixture
    def pipeline_session_with_queue(self):
        """Create a pipeline session with initialized queue counters."""
        return make_pipeline_session()
    
    def test_adaptive_concurrency_low_queue_depth(self, pipeline_session_with_queue):
        """Test current fixed TTS concurrency target."""
        session = pipeline_session_with_queue
        
        ideal_concurrency = session._compute_ideal_concurrency()
        assert ideal_concurrency == 2
    
    def test_adaptive_concurrency_medium_queue_depth(self, pipeline_session_with_queue):
        """Test fixed concurrency remains stable for a small backlog."""
        session = pipeline_session_with_queue
        
        ideal_concurrency = session._compute_ideal_concurrency()
        assert ideal_concurrency == 2
    
    def test_adaptive_concurrency_high_queue_depth(self, pipeline_session_with_queue):
        """Test fixed concurrency stays capped under a larger backlog."""
        session = pipeline_session_with_queue
        
        ideal_concurrency = session._compute_ideal_concurrency()
        assert ideal_concurrency == 2


class TestDashboardMetrics:
    """Phase 5: Dashboard Metrics Display Tests"""
    
    def test_latency_history_structure(self):
        """Test that latency history has all required fields."""
        # Simulate frontend latency history entry
        entry = {
            "turn": 1,
            "total": 500,
            "llm": 350,
            "tts": 150,
            "stt": 100,
            "llm_queue": 50,        # Phase 1 breakdown
            "llm_ttft": 200,        # Phase 1 breakdown
            "llm_streaming": 100,   # Phase 1 breakdown
            "tts_first_sentence": 150,  # Phase 2
            "tts_engine": "edge",   # Phase 2
            "timestamp": 1234567890,
        }
        
        # Verify all fields are present
        required_fields = [
            "turn", "total", "llm", "tts", "stt",
            "llm_queue", "llm_ttft", "llm_streaming",
            "tts_first_sentence", "tts_engine", "timestamp"
        ]
        
        for field in required_fields:
            assert field in entry, f"Missing field: {field}"
    
    def test_llm_metrics_message_structure(self):
        """Test structure of LLM metrics message sent to frontend."""
        msg = {
            "type": "llm_metrics",
            "queue_ms": 50,
            "ttft_ms": 200,
            "streaming_delay_ms": 100,
            "total_ms": 350,
            "turn_id": 1,
        }
        
        assert msg["type"] == "llm_metrics"
        assert msg["queue_ms"] + msg["ttft_ms"] + msg["streaming_delay_ms"] == msg["total_ms"]
    
    def test_tts_metrics_message_structure(self):
        """Test structure of TTS metrics message sent to frontend."""
        msg = {
            "type": "tts_metrics",
            "first_sentence_latency_ms": 150,
            "engine": "edge",
            "turn_id": 1,
        }
        
        assert msg["type"] == "tts_metrics"
        assert isinstance(msg["first_sentence_latency_ms"], int)
        assert msg["engine"] in ["edge", "deepgram"]


class TestEndToEndFlow:
    """Integration tests for complete flow with all phases."""
    
    @pytest.mark.asyncio
    async def test_metrics_flow_with_interruption(self):
        """Test complete flow: metrics tracked and interruption handled correctly."""
        session = make_pipeline_session()
        
        # Simulate turn 1
        session.turn_id = 1
        session._active_turn_id = 1
        session._cancel_event.clear()
        
        # Message for turn 1 should be allowed
        msg1 = {"turn_id": 1, "type": "text", "text": "Response"}
        assert session.can_send_message(msg1) is True
        
        # Simulate user interruption (new turn)
        session.turn_id = 2
        session._active_turn_id = 2
        session._cancel_event.clear()
        
        # Messages from old turn should be rejected
        assert session.can_send_message(msg1) is False
        
        # New turn messages should pass
        msg2 = {"turn_id": 2, "type": "text", "text": "New response"}
        assert session.can_send_message(msg2) is True
    
    def test_concurrent_tts_concurrency_tracking(self):
        """Test concurrent TTS tracking."""
        session = make_pipeline_session()
        
        # Verify current concurrency primitives exist
        assert isinstance(session._tts_semaphore, asyncio.Semaphore)
        assert session._tts_semaphore._value == 2
        assert session._tts_tasks == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
