import os
import json
import asyncio
import datetime
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ── Load the API key from .env (never hardcode it, never expose it to frontend) ──
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY not found. Make sure your .env file exists in this folder "
        "and contains: GEMINI_API_KEY=your_key_here"
    )

MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

# v1beta is the standard Live API endpoint.
GEMINI_WS_URL = (
    f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
    f"v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"
)

app = FastAPI()

# Allow the simple HTML test client (served from file:// or localhost) to connect.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────
# TOOL DEFINITION
# This is the one guided tool the assignment requires (Requirement 3).
# Gemini will call this when the user asks something like "what time is it".
# ─────────────────────────────────────────────────────────────────────────

def get_current_time() -> dict:
    """The actual Python function that runs when Gemini requests the tool."""
    now = datetime.datetime.now().strftime("%I:%M %p on %A, %B %d, %Y")
    return {"current_time": now}


# The JSON schema Gemini needs to know the tool exists. Sent inside the
# setup message, same shape as Module 4's schema.py.
TOOLS_CONFIG = [{
    "functionDeclarations": [{
        "name": "get_current_time",
        "description": "Returns the current local date and time.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
            "required": []
        }
    }]
}]


def build_setup_message() -> dict:
    """The first message that must be sent on the Gemini WebSocket.
    Without this, Gemini rejects the session outright."""
    return {
        "setup": {
            "model": MODEL,
            "generationConfig": {
                "responseModalities": ["AUDIO"]
            },
            "systemInstruction": {
                "parts": [{"text": "You are a helpful voice assistant for The Panel app. Be concise. You do NOT know the current date or time on your own. You MUST call the get_current_time function every single time the user asks about the date or time, without exception. Never guess or state a time from memory."}]
            },
            "tools": [{
                "functionDeclarations": TOOLS_CONFIG[0]["functionDeclarations"]
            }]
        }
    }


@app.get("/health")
def health_check():
    """Quick way to confirm the server is alive: visit http://localhost:8000/health"""
    return {"status": "ok", "message": "Panel voice proxy is running"}


@app.websocket("/ws")
async def proxy_endpoint(client_ws: WebSocket):
    await client_ws.accept()
    print("[FRONTEND] Client connected.")

    try:
        async with websockets.connect(GEMINI_WS_URL) as gemini_ws:
            print("[GEMINI] Connected to Gemini Live API.")

            # ── TODO 1 (done): send setup message FIRST ──
            setup_msg = build_setup_message()
            await gemini_ws.send(json.dumps(setup_msg))
            print("[GEMINI] Setup message sent. Waiting for setupComplete...")

            # Wait for Gemini to confirm the session is ready before doing anything else.
            setup_response = await gemini_ws.recv()
            print(f"[GEMINI] Setup response: {setup_response}")

            # ── TODO 2: client -> gemini loop ──
            async def client_to_gemini():
                """Receives audio (or text, for testing) from the frontend
                and forwards it to Gemini as a realtimeInput message."""
                try:
                    while True:
                        message = await client_ws.receive()

                        if message["type"] == "websocket.disconnect":
                            break

                        if "bytes" in message and message["bytes"] is not None:
                            # Real microphone audio: raw 16-bit PCM, 16kHz, mono.
                            audio_b64 = _b64encode(message["bytes"])
                            payload = {
                                "realtimeInput": {
                                    "mediaChunks": [{
                                        "mimeType": "audio/pcm;rate=16000",
                                        "data": audio_b64
                                    }]
                                }
                            }
                            await gemini_ws.send(json.dumps(payload))

                        elif "text" in message and message["text"] is not None:
                            # Lets us test with typed text before audio is wired,
                            # without needing a microphone at all.
                            text_payload = {
                                "clientContent": {
                                    "turns": [{
                                        "role": "user",
                                        "parts": [{"text": message["text"]}]
                                    }],
                                    "turnComplete": True
                                }
                            }
                            await gemini_ws.send(json.dumps(text_payload))

                except WebSocketDisconnect:
                    print("[FRONTEND] Client disconnected (client_to_gemini).")
                except Exception as e:
                    print(f"[ERROR] client_to_gemini: {e}")

            # ── TODO 3: gemini -> client loop, including tool-call handling ──
            async def gemini_to_client():
                """Receives audio / text / tool-call requests from Gemini
                and either forwards them to the frontend or handles them locally."""
                try:
                    async for raw_message in gemini_ws:
                        # Gemini sends JSON text frames (even for audio --
                        # audio bytes are base64-encoded INSIDE the JSON,
                        # not sent as raw binary frames over this socket).
                        try:
                            data = json.loads(raw_message)
                        except (json.JSONDecodeError, TypeError):
                            print("[GEMINI] Received non-JSON frame, skipping.")
                            continue

                        # --- Tool call interception ---
                        tool_call = data.get("toolCall")
                        if tool_call:
                            print(f"[TOOL CALL] Gemini requested: {tool_call}")
                            function_responses = []

                            for fc in tool_call.get("functionCalls", []):
                                fn_name = fc.get("name")
                                fn_id = fc.get("id")

                                if fn_name == "get_current_time":
                                    result = get_current_time()
                                else:
                                    result = {"error": f"Unknown tool: {fn_name}"}

                                function_responses.append({
                                    "id": fn_id,
                                    "name": fn_name,
                                    "response": result
                                })

                            tool_response_payload = {
                                "toolResponse": {
                                    "functionResponses": function_responses
                                }
                            }
                            await gemini_ws.send(json.dumps(tool_response_payload))
                            print(f"[TOOL RESPONSE] Sent back: {tool_response_payload}")
                            continue  # don't also forward the raw toolCall to frontend

                        # --- Audio / text forwarding to frontend ---
                        server_content = data.get("serverContent")
                        if server_content:
                            model_turn = server_content.get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    inline_data = part.get("inlineData")
                                    if inline_data and inline_data.get("data"):
                                        # Audio comes back as base64 inside JSON.
                                        # Decode it and send as a true binary
                                        # WebSocket frame to the frontend so the
                                        # browser can play it directly.
                                        audio_bytes = _b64decode(inline_data["data"])
                                        await client_ws.send_bytes(audio_bytes)
                                    text_part = part.get("text")
                                    if text_part:
                                        await client_ws.send_text(json.dumps({
                                            "type": "text",
                                            "text": text_part
                                        }))

                            if server_content.get("turnComplete"):
                                await client_ws.send_text(json.dumps({"type": "turnComplete"}))

                except websockets.exceptions.ConnectionClosed:
                    print("[GEMINI] Connection closed.")
                except Exception as e:
                    print(f"[ERROR] gemini_to_client: {e}")

            # ── TODO 4: run both loops concurrently ──
            await asyncio.gather(
                client_to_gemini(),
                gemini_to_client()
            )

    except WebSocketDisconnect:
        print("[FRONTEND] Client disconnected.")
    except Exception as e:
        print(f"[ERROR] proxy_endpoint: {e}")
    finally:
        print("[SESSION] Closed.")


# ── small helpers, kept local so this file has zero extra dependencies ──
import base64

def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")

def _b64decode(data: str) -> bytes:
    return base64.b64decode(data)


if __name__ == "__main__":
    import uvicorn
    print("Starting Panel voice proxy on http://localhost:8000")
    print("Health check: http://localhost:8000/health")
    print("WebSocket endpoint: ws://localhost:8000/ws")
    uvicorn.run(app, host="0.0.0.0", port=8000)
