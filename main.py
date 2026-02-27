"""
Gemini Web Navigator — FastAPI Server
POST /run                → SSE stream of agent steps
POST /stop/{session_id}  → cancel a running session
GET  /health             → health check
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from agent import ActionType, run_navigator

app = FastAPI(
    title="Gemini Web Navigator",
    description="Universal web navigation agent powered by Gemini 2.0 Flash vision",
    version="1.0.0",
)

# Fix 4: CORS middleware — allow all origins so the UI can reach the API from any host
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Fix 3: per-session stop events so the client can cancel a running agent loop
_stop_events: dict[str, asyncio.Event] = {}


class RunRequest(BaseModel):
    goal: str
    start_url: Optional[str] = "https://www.google.com"
    headless: Optional[bool] = True
    session_id: Optional[str] = None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gemini-web-navigator", "version": "1.0.0"}


@app.post("/stop/{session_id}")
async def stop_session(session_id: str):
    """Signal a running agent session to stop after the current step."""
    event = _stop_events.get(session_id)
    if event is None:
        raise HTTPException(404, f"Session '{session_id}' not found or already finished")
    event.set()
    return {"status": "stopping", "session_id": session_id}


@app.post("/run")
async def run_agent(req: RunRequest):
    """
    Run the navigator agent. Streams SSE events:
    - {"type": "step", "step": N, "action": "...", "message": "...", "screenshot": "<base64>", "elapsed_ms": N}
    - {"type": "done", "message": "..."}
    - {"type": "error", "message": "..."}

    Pass a unique `session_id` in the request body to enable cancellation via
    POST /stop/{session_id}.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY environment variable not set")

    # Fix 3: create and register a stop event for this session
    session_id = req.session_id or str(id(req))
    stop_event = asyncio.Event()
    _stop_events[session_id] = stop_event

    async def stream():
        try:
            async for result in run_navigator(
                goal=req.goal,
                start_url=req.start_url,
                api_key=GEMINI_API_KEY,
                headless=req.headless,
                stop_event=stop_event,
            ):
                # Fix 3: also check stop_event here so we can break the SSE stream
                if stop_event.is_set():
                    yield f"data: {json.dumps({'type': 'stopped', 'message': 'Session cancelled by client'})}\n\n"
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
            # Fix 3: clean up the stop event after the loop ends
            _stop_events.pop(session_id, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the web UI."""
    with open("static/index.html") as f:
        return f.read()
