import os
import json
import asyncio
import sqlite3
import requests
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load the API key from .env (never hardcode it, never expose it to frontend)
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY not found. Make sure your .env file exists in this folder "
        "and contains: GEMINI_API_KEY=your_key_here"
    )

MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

GEMINI_WS_URL = (
    f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
    f"v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────
# TOOL A: get_interview_question — real SQLite database query
# ─────────────────────────────────────────────────────────────────────

def get_interview_question(topic: str = None) -> dict:
    """Fetches a real question from the local questions.db SQLite database."""
    try:
        conn = sqlite3.connect("questions.db")
        cursor = conn.cursor()

        if topic:
            cursor.execute(
                "SELECT id, question FROM questions WHERE topic = ? ORDER BY asked_count ASC, RANDOM() LIMIT 1",
                (topic,)
            )
        else:
            cursor.execute(
                "SELECT id, question FROM questions ORDER BY asked_count ASC, RANDOM() LIMIT 1"
            )

        row = cursor.fetchone()

        if row is None:
            conn.close()
            return {"error": f"No questions found for topic '{topic}'."}

        question_id, question_text = row

        cursor.execute(
            "UPDATE questions SET asked_count = asked_count + 1 WHERE id = ?",
            (question_id,)
        )
        conn.commit()
        conn.close()

        return {"question": question_text}

    except sqlite3.Error as e:
        return {"error": f"Database error: {e}"}


GET_QUESTION_TOOL = {
    "name": "get_interview_question",
    "description": (
        "Fetches a real UPSC interview question from the question bank. "
        "Call this to ask the candidate a new question. Optionally specify "
        "a topic (Polity, Economy, Geography, CurrentAffairs, or Ethics) "
        "if the conversation has been focused on one area; otherwise omit "
        "topic to get any question."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "STRING",
                "description": "Optional topic filter: Polity, Economy, Geography, CurrentAffairs, or Ethics"
            }
        },
        "required": []
    }
}


# ─────────────────────────────────────────────────────────────────────
# TOOL B: check_fact — real Wikipedia API call (interviewer-side verify)
# ─────────────────────────────────────────────────────────────────────

def check_fact(topic: str) -> dict:
    """Fetches a real summary from Wikipedia's API for a given topic."""
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{topic}"
        response = requests.get(url, timeout=10)

        if response.status_code == 404:
            return {"error": f"No Wikipedia entry found for '{topic}'."}

        response.raise_for_status()
        data = response.json()

        return {
            "summary": data.get("extract", "No summary available."),
            "title": data.get("title", topic)
        }

    except requests.exceptions.RequestException:
        return {"error": "Fact-check service currently unavailable."}


CHECK_FACT_TOOL = {
    "name": "check_fact",
    "description": (
        "Looks up a factual summary of a topic from Wikipedia to silently "
        "verify the accuracy of the candidate's answer. Use this internally "
        "to judge correctness — do NOT tell the candidate you are looking "
        "something up; use the result to decide whether to challenge a "
        "vague or incorrect answer."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "STRING",
                "description": "The topic or claim to fact-check, e.g. 'Article 370' or 'fiscal deficit'"
            }
        },
        "required": ["topic"]
    }
}


# ─────────────────────────────────────────────────────────────────────
# PERSONA — strict UPSC interview board member
# ─────────────────────────────────────────────────────────────────────

PERSONA_PROMPT = """
# IDENTITY
You are a senior UPSC interview board member conducting a mock Civil
Services personality test interview. You are formal, precise, and
unimpressed by vague or rehearsed answers. This is a VOICE conversation —
speak in natural spoken sentences, never read out lists or markdown.

# OPERATIONAL BOUNDARY
You ONLY conduct interview questions, ask follow-ups, and evaluate answers.
You do not chat about unrelated topics. If the candidate goes off-topic,
redirect them back to the interview in one sentence.

# CORE BEHAVIOR — DO NOT LET ANSWERS OFF THE HOOK
If a candidate's answer is vague, generic, or avoids the actual question,
you must press them: ask for a specific example, a number, a named case,
or a clearer definition. Do not accept "it depends" or "there are many
factors" as a complete answer — ask them to commit to a position and
defend it.

If you are unsure whether a candidate's factual claim is accurate, use the
check_fact tool silently to verify before responding. If their claim is
wrong, correct them directly but respectfully — do not let an incorrect
fact pass unchallenged.

Use get_interview_question whenever you need to ask a new question, or to
move to a new topic after a candidate has answered the current one
adequately.

# TONE
Calm, formal, occasionally pointed. Not unkind, but not warm either — this
is a real interview, not a friendly chat. Short, direct sentences.

# GUARDRAILS
Never invent facts. If check_fact fails or returns nothing useful, say so
plainly rather than guessing. If a tool call fails, tell the candidate
there was a technical issue and continue the interview without it.
"""


def build_setup_message() -> dict:
    return {
        "setup": {
            "model": MODEL,
            "generationConfig": {
                "responseModalities": ["AUDIO"]
            },
            "systemInstruction": {
                "parts": [{"text": PERSONA_PROMPT}]
            },
            "tools": [{
                "functionDeclarations": [GET_QUESTION_TOOL, CHECK_FACT_TOOL]
            }]
        }
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "Panel voice proxy is running"}


@app.websocket("/ws")
async def proxy_endpoint(client_ws: WebSocket):
    await client_ws.accept()
    print("[FRONTEND] Client connected.")

    # Shared flag so either loop can tell the other the client is gone.
    client_alive = {"ok": True}

    try:
        async with websockets.connect(GEMINI_WS_URL) as gemini_ws:
            print("[GEMINI] Connected to Gemini Live API.")

            setup_msg = build_setup_message()
            await gemini_ws.send(json.dumps(setup_msg))
            print("[GEMINI] Setup message sent. Waiting for setupComplete...")

            setup_response = await gemini_ws.recv()
            print(f"[GEMINI] Setup response: {setup_response}")

            async def client_to_gemini():
                """Frontend -> Gemini: forward mic audio (or typed text)."""
                try:
                    while True:
                        message = await client_ws.receive()

                        if message["type"] == "websocket.disconnect":
                            client_alive["ok"] = False
                            break

                        if "bytes" in message and message["bytes"] is not None:
                            audio_b64 = _b64encode(message["bytes"])
                            payload = {
                                "realtimeInput": {
                                    "audio": {
                                        "data": audio_b64,
                                        "mimeType": "audio/pcm;rate=16000"
                                    }
                                }
                            }
                            await gemini_ws.send(json.dumps(payload))

                        elif "text" in message and message["text"] is not None:
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
                    client_alive["ok"] = False
                except Exception as e:
                    print(f"[ERROR] client_to_gemini: {e}")
                    client_alive["ok"] = False

            async def gemini_to_client():
                """Gemini -> Frontend: forward audio/text, handle tool calls
                and barge-in. A single failed send never kills this loop —
                that is what keeps barge-in alive across the whole session."""
                try:
                    async for raw_message in gemini_ws:
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
                                fn_args = fc.get("args", {})

                                if fn_name == "get_interview_question":
                                    result = get_interview_question(**fn_args)
                                elif fn_name == "check_fact":
                                    result = check_fact(**fn_args)
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
                            continue

                        server_content = data.get("serverContent")

                        # --- BARGE-IN: handle FIRST, before any audio ---
                        if server_content and server_content.get("interrupted"):
                            print("[BARGE-IN] User interrupted. Flushing playback.")
                            await _safe_send_text(client_ws, json.dumps({"type": "flush"}), client_alive)
                            continue

                        # --- Audio / text forwarding to frontend ---
                        if server_content:
                            model_turn = server_content.get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    inline_data = part.get("inlineData")
                                    if inline_data and inline_data.get("data"):
                                        audio_bytes = _b64decode(inline_data["data"])
                                        await _safe_send_bytes(client_ws, audio_bytes, client_alive)
                                    text_part = part.get("text")
                                    if text_part:
                                        await _safe_send_text(
                                            client_ws,
                                            json.dumps({"type": "text", "text": text_part}),
                                            client_alive
                                        )

                            if server_content.get("turnComplete"):
                                await _safe_send_text(
                                    client_ws,
                                    json.dumps({"type": "turnComplete"}),
                                    client_alive
                                )

                except websockets.exceptions.ConnectionClosed:
                    print("[GEMINI] Connection closed.")
                except Exception as e:
                    print(f"[ERROR] gemini_to_client: {e}")

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


# ─────────────────────────────────────────────────────────────────────
# Safe send helpers: a failed send (client gone) is swallowed quietly so
# it never crashes the receive loop or leaves Gemini in a bad state.
# ─────────────────────────────────────────────────────────────────────

async def _safe_send_bytes(client_ws, data, client_alive):
    if not client_alive["ok"]:
        return
    try:
        await client_ws.send_bytes(data)
    except Exception:
        client_alive["ok"] = False

async def _safe_send_text(client_ws, text, client_alive):
    if not client_alive["ok"]:
        return
    try:
        await client_ws.send_text(text)
    except Exception:
        client_alive["ok"] = False


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