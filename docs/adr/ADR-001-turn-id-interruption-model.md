# ADR-001 — Turn-ID Interruption Model

**Status:** Accepted  
**Date:** 2026-06-23

## Context

Voice agents must handle barge-in: the user starts speaking while the AI is still
responding. Without explicit tracking, in-flight audio buffers, LLM streams, and TTS
synthesis from the cancelled turn can bleed into the new turn, causing audio overlap
and corrupted transcript state.

## Decision

Every pipeline turn is assigned a monotonically incrementing integer `turn_id`. This
ID is stamped on every outbound message (JSON and binary audio frames). Both the server
and client gate delivery on the active turn ID:

- **Server (`pipeline.py`):** `_can_send(turn_id)` checks `_cancel_event` and compares
  `turn_id == _active_turn_id` before queuing any message. Audio chunks routed through
  `_send_for_turn()` so they receive the same gate as JSON messages.
- **Client (`audio-output.js`):** Four sequential guard checks (epoch + generation +
  turn_id) before decode, before playback scheduling, and inside `_schedulePlayback`.

On interruption, the server increments `_active_turn_id` (or sets it to `None`) and
sets `_cancel_event`. The client increments `audioEpoch` and `decodeGeneration`. Any
audio chunk that passed the gate before the cancel but arrives after it is discarded
at the client-side guards.

## Consequences

- Sub-millisecond interruption response on the server; no audio overlap in practice.
- Small bookkeeping overhead (integer comparison per message).
- Audio sent via `send_message()` directly (not `_send_for_turn()`) bypasses the gate —
  all audio must use `_send_for_turn()` to maintain safety.
