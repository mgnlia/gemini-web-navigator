"""
Gemini Web Navigator — FastAPI Server
POST /run/{session_id}  → SSE stream of agent steps
POST /stop/{session_id} → cancel a running session
GET  /health            → health check
GET  /                  → web UI
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from agent import ActionType, run_navigator

app = FastAPI(
    title="Gemini Web Navigator",
    description="Universal web navigation agent powered by Gemini 2.0 Flash vision",
    version="1.1.0",
)

# Fix 4 (already present — verified): CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Fix 3: session stop registry — maps session_id → asyncio.Event
_stop_events: dict[str, asyncio.Event] = {}


class RunRequest(BaseModel):
    goal: str
    start_url: Optional[str] = "https://www.google.com"
    headless: Optional[bool] = True
    session_id: Optional[str] = None  # caller may supply; server generates if absent


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gemini-web-navigator", "version": "1.1.0"}


@app.post("/run")
async def run_agent(req: RunRequest):
    """
    Run the navigator agent. Streams SSE events:
    - {"type": "session", "session_id": "<id>"}   — first event, use for /stop
    - {"type": "step", "step": N, "action": "...", "message": "...", "screenshot": "<base64>", "elapsed_ms": N}
    - {"type": "done", "message": "..."}
    - {"type": "error", "message": "..."}
    - {"type": "stopped"}                          — emitted when /stop was called
    """
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY environment variable not set")

    session_id = req.session_id or str(uuid.uuid4())
    stop_event = asyncio.Event()
    _stop_events[session_id] = stop_event

    async def stream():
        try:
            # First event: hand the session_id to the client so it can call /stop
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

            async for result in run_navigator(
                goal=req.goal,
                start_url=req.start_url,
                api_key=GEMINI_API_KEY,
                headless=req.headless,
                stop_event=stop_event,
            ):
                # Check stop between steps
                if stop_event.is_set():
                    yield f"data: {json.dumps({'type': 'stopped'})}\n\n"
                    break

                event = {
                    "type": "step",
                    "step": result.step,
                    "action": result.action.type.value,
                    "reason": result.action.reason or "",
                    "message": result.message,
                    "success": result.success,
                    "screenshot": result.screenshot_b64,
                    "elapsed_ms": result.elapsed_ms,
                }
                yield f"data: {json.dumps(event)}\n\n"

                if result.action.type == ActionType.DONE:
                    yield f"data: {json.dumps({'type': 'done', 'message': result.action.reason})}\n\n"
                    break
                elif result.action.type == ActionType.FAIL:
                    yield f"data: {json.dumps({'type': 'error', 'message': result.action.reason})}\n\n"
                    break

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            # Clean up stop event registry
            _stop_events.pop(session_id, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/stop/{session_id}")
async def stop_session(session_id: str):
    """
    Signal a running session to stop cleanly after the current step.
    Fix 3: AbortController pattern via asyncio.Event.
    """
    event = _stop_events.get(session_id)
    if event is None:
        raise HTTPException(404, f"Session '{session_id}' not found or already finished")
    event.set()
    return {"status": "stopping", "session_id": session_id}


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the web UI."""
    with open("static/index.html") as f:
        return f.read()
