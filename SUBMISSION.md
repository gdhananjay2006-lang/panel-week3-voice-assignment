# Submission — The Smart Echo Box (Module 5)

## Architecture

`server.py` is a FastAPI backend that sits between a browser client and
Gemini's Live API. It holds two WebSocket connections open per session: one
to the frontend (`ws://localhost:8000/ws`) and one to Gemini's
`BidiGenerateContent` endpoint. Both stay open for the life of the session,
and two `async` loops — one per direction — run concurrently via
`asyncio.gather()` so audio can flow both ways without either side blocking
the other.

## Data flow

- **Client → Gemini:** the browser captures mic audio (16-bit PCM, 16kHz)
  and sends it as binary WebSocket frames. The proxy base64-encodes each
  chunk and wraps it in a `realtimeInput` message for Gemini.
- **Gemini → client:** Gemini sends JSON text frames. Audio replies arrive
  as base64 data *inside* that JSON, not as raw binary frames — the proxy
  decodes it and re-sends it to the browser as a true binary frame, which
  the client plays via the Web Audio API.
- **Tool calling:** when a response contains a `toolCall`, the proxy
  intercepts it before forwarding anything, runs the matching Python
  function (`get_current_time()`), and sends the result back as a
  `toolResponse`. Gemini then speaks the real result.

## Handling the dual connections

Both connections live inside one `async with websockets.connect(...)` block
per client session. `client_to_gemini()` and `gemini_to_client()` are two
coroutines run together with `asyncio.gather()`, so neither has to wait on
the other. A `try/except/finally` around the whole endpoint cleans up the
session if either side disconnects.

## Security

The API key only ever lives in `.env`, loaded via `python-dotenv`. The
frontend has no knowledge of the key or the Gemini URL — it only ever talks
to `localhost:8000`.

## Challenges I hit

**The Module 2 echo server was dead.** `echo.websocket.events` didn't
resolve at all — confirmed with `ping`, not just a connection failure. Swapped
to Postman's public echo endpoint instead, which proves the same thing
(one connection, multiple round trips).

**New Gemini key format.** Google is migrating from `AIza...` keys to a new
`AQ....` auth-key format, and every key I generated came out in the new
format. Took a minute to confirm this was expected (not a broken key) —
turned out it authenticates the same way, no code change needed.

**The model skipped the tool call on the first real test.** I asked "what
time is it?" and Gemini answered directly with a guessed, wrong time instead
of calling `get_current_time`. Fixed by making the system instruction
explicit: the model does not know the time on its own and must call the
function every time. After that, it called the tool correctly and
consistently — confirmed in the server logs, which show the real
`functionCalls` payload and the actual timestamp from Python's
`datetime.now()` being spoken back correctly.

## Verification

All four requirements were tested using the real microphone path, not just
typed text:

- Asked a general question by voice ("who is the Prime Minister of India?")
  and got a correct, audible answer — confirms the full mic → proxy →
  Gemini → speaker pipeline works with real voice, not just text.
- Asked "what time is it?" by voice and confirmed in the server logs that
  the tool call fired through the voice path specifically, ran the real
  function, and returned the correct time, which Gemini spoke back
  correctly.

The typed-text input in the test client was only used early on, to confirm
the backend logic worked before adding audio capture as another variable.
Everything above was re-verified using real voice once the backend was
proven correct.
