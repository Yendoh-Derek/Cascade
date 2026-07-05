# WebSocket Protocol Reference

Cascade uses a single WebSocket endpoint at `/ws` for the full-duplex voice pipeline.

## Connection

- Endpoint: `/ws`
- Query parameter: `tts_engine=deepgram|edge`
- The server accepts binary PCM16 audio frames from the client and emits both binary audio frames and JSON control messages.

## Authentication handshake

If `CASCADE_AUTH_SECRET` is set, the server sends a `challenge` message with a nonce before any pipeline work begins. The client must reply with a JSON message shaped like:

```json
{ "type": "auth", "response": "<hmac-sha256>" }
```

On success, the server replies with `auth_ok`. If authentication fails or times out, the server closes the connection with an error message.

## Client → server messages

### Binary audio

- Raw PCM16 audio bytes captured from the browser microphone.
- The server forwards these bytes to the STT pipeline.

### Text/JSON control messages

- `cancel` — interrupt the active turn and start a new one.
- `finalize` — flush any pending STT audio to the current utterance.
- `auth` — respond to the challenge when auth is enabled.
- `pong` — reply to a server-side ping.
- `client_latency` — report client-perceived latency data.
- `playback_finished` — notify the server that playback of the current turn has finished.

## Server → client messages

- `challenge` — issued at the start of auth when the secret is configured.
- `auth_ok` — authentication successful.
- `busy` — server capacity limit reached.
- `ping` — periodic keepalive message.
- `transcript` — finalized transcript for the current turn.
- `response_chunk` — incremental streaming text chunk.
- `response_end` — end of the current assistant response.
- `latency` — latency snapshot for the turn.
- `turn_cancelled` — an in-progress turn was interrupted.
- Binary audio — synthesized audio chunks emitted to the client.

## Notes

- The server uses a turn-id gate so outdated frames and stale audio are dropped after a turn is cancelled or replaced.
- For local development, the auth secret can be left unset and the handshake is skipped.
