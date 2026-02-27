"""
Gemini Web Navigator — FastAPI Server
POST /run  → SSE stream of agent steps
GET  /health → health check
"""

from __future__ import annotations

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


class RunRequest(BaseModel):
    goal: str
    start_url: Optional[str] = "https://www.google.com"
    headless: Optional[bool] = True


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gemini-web-navigator", "version": "1.0.0"}


@app.post("/run")
async def run_agent(req: RunRequest):
    """
    Run the navigator agent. Streams SSE events:
    - {"type": "step", "step": N, "action": "...", "message": "...", "screenshot": "<base64>", "elapsed_ms": N}
    - {"type": "done", "message": "..."}
    - {"type": "error", "message": "..."}
    """
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY environment variable not set")

    async def stream():
        try:
            async for result in run_navigator(
                goal=req.goal,
                start_url=req.start_url,
                api_key=GEMINI_API_KEY,
                headless=req.headless,
            ):
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
