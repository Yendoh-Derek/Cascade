# ADR-005: TTS Voice Selector Removed from Menu Bar

**Date:** 2026-07-08
**Status:** Accepted
**Author:** Derek (Yendoh)

---

## Context

The bottom menu bar previously displayed a segmented toggle allowing users to switch
between two TTS engines at runtime:

- **Deepgram Aura 2** — Low-latency, high-quality neural voice (primary, recommended)
- **Edge TTS** — Microsoft's free browser-based fallback (higher latency, no API key required)

While useful during development and testing, the selector added visual complexity to the
menu bar and was not needed for typical production use. Deepgram Aura 2 is the preferred
engine for all deployments where a `DEEPGRAM_API_KEY` is available.

---

## Decision

Remove the TTS engine selector from the UI entirely. The menu bar now shows only the
three action buttons (Start/Stop, Reset, Charts), centered horizontally.

**Deepgram Aura 2 remains the default and only active TTS engine.**

The selector code is fully preserved — commented out — in all three files so it can
be trivially re-enabled without any rewrite.

---

## Consequences

- Cleaner, less cluttered menu bar.
- Fewer cognitive choices for end users.
- Deepgram Aura 2 is always used; Edge TTS is effectively disabled at the UI level
  (the backend still supports it via the `?tts_engine=edge` WebSocket query parameter).
- If Deepgram is unavailable, a developer must follow the re-enable steps below
  or set the engine server-side.

---

## How to Re-Enable the TTS Selector (Edge TTS Fallback)

Follow these **three steps** to restore the full voice-selector UI:

### Step 1 — `frontend/index.html`

Find the `[TTS SELECTOR — DISABLED]` comment block inside `<footer>` and
un-comment the `.menu-bar-left` div:

```html
<!-- BEFORE (disabled) -->
<!--
[TTS SELECTOR — DISABLED]
...
<div class="menu-bar-left">
  ...
</div>
-->

<!-- AFTER (re-enabled) -->
<div class="menu-bar-left">
  <span class="tts-label">Voice</span>
  <div class="tts-toggle-group">
    <button class="tts-toggle-btn active" id="tts-btn-deepgram"
            data-engine="deepgram" aria-pressed="true"
            title="Deepgram Aura 2 — low latency (recommended)">
      Aura 2
    </button>
    <button class="tts-toggle-btn" id="tts-btn-edge"
            data-engine="edge" aria-pressed="false"
            title="Edge TTS — free fallback (higher latency)">
      Edge (fallback)
    </button>
  </div>
</div>
```

Also **remove** the `menu-bar--centered` class from `<footer>`:

```html
<!-- BEFORE -->
<footer class="menu-bar menu-bar--centered glass">

<!-- AFTER -->
<footer class="menu-bar glass">
```

And change `.menu-bar-center` back to `.menu-bar-right` for the button group:

```html
<!-- BEFORE -->
<div class="menu-bar-center">

<!-- AFTER -->
<div class="menu-bar-right">
```

---

### Step 2 — `frontend/ui.js`

Find the large `/* ADR-005: TTS Engine Selector Toggles — DISABLED ... */` comment block
and un-comment the inner `// Custom TTS Engine Selector Toggles` section.

It begins at the line:
```js
// Custom TTS Engine Selector Toggles
document.querySelectorAll(".tts-toggle-btn").forEach((btn) => {
```

Remove the outer `/*` ... `*/` wrapper around this block.

---

### Step 3 — `frontend/style.css` (no change needed)

The `.tts-label`, `.tts-toggle-group`, and `.tts-toggle-btn` styles are already
present in `style.css` and will take effect automatically once the HTML is restored.

The `.menu-bar--centered` modifier can also remain — it simply won't be applied
once removed from the `<footer>` class list.

---

## Engine Reference

| Engine          | `data-engine` value | Backend param          | Latency | API Key Required         |
|-----------------|---------------------|------------------------|---------|--------------------------|
| Deepgram Aura 2 | `deepgram`          | `?tts_engine=deepgram` | Low     | Yes (`DEEPGRAM_API_KEY`) |
| Edge TTS        | `edge`              | `?tts_engine=edge`     | Higher  | No                       |

> **Tip:** You can also force a specific engine directly via the WebSocket URL by
> appending `?tts_engine=edge` to the connection string in `frontend/transport.js`,
> bypassing the UI selector entirely — useful for server-side overrides or testing.

---

## Related

- [ADR-003: Deepgram Speak Many Flush Once](./ADR-003-deepgram-speak-many-flush-once.md)
- `frontend/transport.js` — passes `?tts_engine=<value>` on WebSocket connect
- `frontend/app.js` — `selectedTTSEngine` defaults to `"deepgram"` via `localStorage` fallback
