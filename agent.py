"""
Gemini Web Navigator — Core Agent Loop
Screenshot → Gemini 2.0 Flash vision → Action → Execute → Repeat
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from typing import AsyncGenerator, Optional

from google import genai
from google.genai import types
from playwright.async_api import Page, async_playwright

# ── Config ────────────────────────────────────────────────────────────────────

MODEL = "gemini-2.0-flash-exp"
MAX_STEPS = 25
SCREENSHOT_WIDTH = 1280
SCREENSHOT_HEIGHT = 800


class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    NAVIGATE = "navigate"
    WAIT = "wait"
    DONE = "done"
    FAIL = "fail"


@dataclass
class Action:
    type: ActionType
    x: Optional[int] = None
    y: Optional[int] = None
    text: Optional[str] = None
    url: Optional[str] = None
    direction: Optional[str] = None  # up/down
    amount: Optional[int] = None     # scroll pixels
    reason: Optional[str] = None


@dataclass
class StepResult:
    step: int
    screenshot_b64: str
    action: Action
    success: bool
    message: str
    elapsed_ms: int


# ── Gemini Vision ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a web navigation agent. You observe browser screenshots and decide what action to take to accomplish the user's goal.

You MUST respond with a single JSON object — no markdown, no explanation outside the JSON.

Available actions:
- {"action": "click", "x": <int>, "y": <int>, "reason": "<why>"}
- {"action": "type", "text": "<text to type>", "reason": "<why>"}
- {"action": "scroll", "direction": "down"|"up", "amount": 300, "reason": "<why>"}
- {"action": "navigate", "url": "<full URL>", "reason": "<why>"}
- {"action": "wait", "reason": "<why>"}
- {"action": "done", "reason": "<what was accomplished>"}
- {"action": "fail", "reason": "<why it cannot be done>"}

Rules:
1. Analyze the screenshot carefully — read text, identify buttons, forms, links
2. Choose the single most effective next action toward the goal
3. For clicks, use pixel coordinates (x=0,y=0 is top-left, x=1280,y=800 is bottom-right)
4. For typing, assume the correct field is already focused (after clicking it)
5. If the goal is achieved, respond with "done"
6. If blocked by CAPTCHA or login wall you cannot pass, respond with "fail"
7. NEVER access DOM or APIs — only use what you see in the screenshot
"""


def _build_gemini_request(
    client: genai.Client,
    image_bytes: bytes,
    prompt: str,
) -> str:
    """Call Gemini and return the raw text response."""
    response = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/png",
                            data=image_bytes,  # raw bytes — no PIL re-encoding
                        )
                    ),
                    types.Part(text=prompt),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
            max_output_tokens=512,
        ),
    )
    return response.text.strip()


def _strip_markdown(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return raw


async def get_next_action(
    client: genai.Client,
    screenshot_b64: str,
    goal: str,
    history: list[dict],
) -> Action:
    """Send screenshot to Gemini 2.0 Flash and get the next action.

    Fix 1: screenshot bytes are sent directly — no PIL decode/re-encode round-trip.
    Fix 2: JSON parse failures retry up to 3 times with fresh Gemini calls before
           falling back to a FAIL action instead of raising.
    """

    # Build history context (last 5 steps)
    history_text = ""
    if history:
        recent = history[-5:]
        history_text = "\n\nRecent actions taken:\n" + "\n".join(
            f"Step {h['step']}: {h['action']} — {h['message']}" for h in recent
        )

    prompt = f"Goal: {goal}{history_text}\n\nWhat is the next action to take? Respond with JSON only."

    # Fix 1: decode once — send raw bytes, skip PIL re-encoding
    image_bytes = base64.b64decode(screenshot_b64)

    # Fix 2: retry loop — re-call Gemini on JSON parse failure
    last_err: Exception | None = None
    raw: str = ""
    for attempt in range(3):
        try:
            raw = _build_gemini_request(client, image_bytes, prompt)
            raw = _strip_markdown(raw)
            data = json.loads(raw)
            break
        except json.JSONDecodeError as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
        except Exception as e:
            # Non-JSON error (network, API quota, etc.) — don't retry
            return Action(type=ActionType.FAIL, reason=f"Gemini error: {e}")
    else:
        return Action(
            type=ActionType.FAIL,
            reason=f"JSON parse failed after 3 retries: {last_err}. Raw: {raw[:200]}",
        )

    action_type = ActionType(data.get("action", "fail"))

    return Action(
        type=action_type,
        x=data.get("x"),
        y=data.get("y"),
        text=data.get("text"),
        url=data.get("url"),
        direction=data.get("direction"),
        amount=data.get("amount", 300),
        reason=data.get("reason", ""),
    )


# ── Browser Actions ───────────────────────────────────────────────────────────

async def take_screenshot(page: Page) -> str:
    """Take a screenshot and return as base64 PNG."""
    await page.set_viewport_size({"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT})
    screenshot_bytes = await page.screenshot(type="png", full_page=False)
    return base64.b64encode(screenshot_bytes).decode()


async def execute_action(page: Page, action: Action) -> tuple[bool, str]:
    """Execute a browser action. Returns (success, message)."""
    try:
        if action.type == ActionType.CLICK:
            await page.mouse.click(action.x, action.y)
            await page.wait_for_load_state("networkidle", timeout=5000)
            return True, f"Clicked at ({action.x}, {action.y})"

        elif action.type == ActionType.TYPE:
            await page.keyboard.type(action.text, delay=50)
            return True, f"Typed: {action.text[:50]}..."

        elif action.type == ActionType.SCROLL:
            delta = action.amount if action.direction == "down" else -action.amount
            await page.mouse.wheel(0, delta)
            await asyncio.sleep(0.5)
            return True, f"Scrolled {action.direction} {action.amount}px"

        elif action.type == ActionType.NAVIGATE:
            await page.goto(action.url, wait_until="networkidle", timeout=30000)
            return True, f"Navigated to {action.url}"

        elif action.type == ActionType.WAIT:
            await asyncio.sleep(2)
            return True, "Waited 2 seconds"

        elif action.type == ActionType.DONE:
            return True, f"Goal accomplished: {action.reason}"

        elif action.type == ActionType.FAIL:
            return False, f"Cannot complete: {action.reason}"

    except Exception as e:
        return False, f"Action failed: {e}"

    return False, "Unknown action"


# ── Main Agent Loop ───────────────────────────────────────────────────────────

async def run_navigator(
    goal: str,
    start_url: str = "https://www.google.com",
    api_key: str = "",
    headless: bool = True,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[StepResult, None]:
    """
    Core agent loop: screenshot → Gemini vision → action → execute → repeat.
    Yields StepResult for each step (for SSE streaming).

    Args:
        stop_event: optional asyncio.Event; when set, the loop exits cleanly
                    after the current step completes (Fix 3 — AbortController).
    """
    client = genai.Client(api_key=api_key)
    history: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Navigate to start URL
        await page.goto(start_url, wait_until="networkidle", timeout=30000)

        for step in range(1, MAX_STEPS + 1):
            # Fix 3: honour stop signal before each step
            if stop_event is not None and stop_event.is_set():
                break

            t0 = time.time()

            # 1. Screenshot
            screenshot_b64 = await take_screenshot(page)

            # 2. Gemini vision → action
            action = await get_next_action(client, screenshot_b64, goal, history)

            # 3. Execute
            success, message = await execute_action(page, action)

            elapsed = int((time.time() - t0) * 1000)
            result = StepResult(
                step=step,
                screenshot_b64=screenshot_b64,
                action=action,
                success=success,
                message=message,
                elapsed_ms=elapsed,
            )

            history.append({
                "step": step,
                "action": action.type.value,
                "message": message,
            })

            yield result

            # Stop conditions
            if action.type in (ActionType.DONE, ActionType.FAIL):
                break

            if not success and action.type not in (ActionType.WAIT,):
                await asyncio.sleep(2)

        await browser.close()


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    goal = " ".join(sys.argv[1:]) or "Search for 'Gemini AI' on Google and click the first result"
    api_key = os.environ.get("GEMINI_API_KEY", "")

    async def main():
        async for result in run_navigator(goal, api_key=api_key, headless=False):
            print(f"Step {result.step}: [{result.action.type.value}] {result.message} ({result.elapsed_ms}ms)")
            if result.action.type in (ActionType.DONE, ActionType.FAIL):
                print(f"\nFinal: {result.action.reason}")
                break

    asyncio.run(main())
