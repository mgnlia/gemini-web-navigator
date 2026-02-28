"""
Gemini Web Navigator — Core Agent Loop
Uses Gemini 2.5 Computer Use model with built-in UI action tools.
Screenshot → Gemini Computer Use → Action → Execute → Repeat
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Optional

from google import genai
from google.genai import types
from playwright.async_api import Page, async_playwright

# ── Config ────────────────────────────────────────────────────────────────────

MODEL = "gemini-2.5-computer-use-preview-10-2025"
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
    SCREENSHOT = "screenshot"
    KEY = "key"
    MOVE_MOUSE = "move_mouse"
    DRAG = "drag"


@dataclass
class Action:
    type: ActionType
    x: Optional[int] = None
    y: Optional[int] = None
    text: Optional[str] = None
    url: Optional[str] = None
    direction: Optional[str] = None
    amount: Optional[int] = None
    reason: Optional[str] = None
    # Computer Use extras
    key: Optional[str] = None
    coordinate: Optional[list] = None
    start_coordinate: Optional[list] = None


@dataclass
class StepResult:
    step: int
    screenshot_b64: str
    action: Action
    success: bool
    message: str
    elapsed_ms: int


# ── Gemini Computer Use ───────────────────────────────────────────────────────

async def get_next_action(
    client: genai.Client,
    screenshot_b64: str,
    goal: str,
    history: list[dict],
    conversation: list,
) -> tuple[Action, list]:
    """
    Call Gemini 2.5 Computer Use model with screenshot and get the next action.
    Uses built-in Computer Use tool — no custom JSON parsing needed.
    Returns (Action, updated_conversation).
    """
    image_bytes = base64.b64decode(screenshot_b64)

    # Build the user turn with screenshot
    user_parts = [
        types.Part(
            inline_data=types.Blob(
                mime_type="image/png",
                data=image_bytes,
            )
        ),
    ]

    # On first step, include the goal; subsequent steps just send the screenshot
    if not conversation:
        user_parts.append(types.Part(text=f"Goal: {goal}\n\nPlease analyze this screenshot and take the next action to accomplish the goal."))
    else:
        user_parts.append(types.Part(text="Here is the current state of the browser. Continue working toward the goal."))

    conversation.append(types.Content(role="user", parts=user_parts))

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=conversation,
            config=types.GenerateContentConfig(
                tools=[types.Tool(computer_use=types.ToolComputerUse(
                    environment=types.Environment.ENVIRONMENT_BROWSER
                ))],
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
    except Exception as e:
        return Action(type=ActionType.FAIL, reason=f"Gemini API error: {e}"), conversation

    # Append model response to conversation
    conversation.append(response.candidates[0].content)

    # Extract the function call (Computer Use action)
    action = _parse_computer_use_response(response)
    return action, conversation


def _parse_computer_use_response(response) -> Action:
    """Parse Gemini Computer Use response into an Action."""
    candidate = response.candidates[0]
    content = candidate.content

    # Check finish reason for done/stop signals
    finish_reason = candidate.finish_reason
    if finish_reason and finish_reason.name in ("STOP", "MAX_TOKENS"):
        # Check if there's text indicating completion
        for part in content.parts:
            if hasattr(part, 'text') and part.text:
                text_lower = part.text.lower()
                if any(w in text_lower for w in ["completed", "accomplished", "done", "finished", "found"]):
                    return Action(type=ActionType.DONE, reason=part.text[:200])

    # Look for function call parts
    for part in content.parts:
        if hasattr(part, 'function_call') and part.function_call:
            fc = part.function_call
            name = fc.name
            args = dict(fc.args) if fc.args else {}

            if name == "computer_use_click":
                coord = args.get("coordinate", [0, 0])
                return Action(
                    type=ActionType.CLICK,
                    x=int(coord[0]) if coord else 0,
                    y=int(coord[1]) if coord else 0,
                    reason=args.get("reason", "click"),
                )
            elif name == "computer_use_type":
                return Action(
                    type=ActionType.TYPE,
                    text=args.get("text", ""),
                    reason="type text",
                )
            elif name == "computer_use_scroll":
                coord = args.get("coordinate", [640, 400])
                direction = args.get("direction", "down")
                amount = args.get("amount", 3)
                return Action(
                    type=ActionType.SCROLL,
                    x=int(coord[0]) if coord else 640,
                    y=int(coord[1]) if coord else 400,
                    direction=direction,
                    amount=int(amount) * 100,
                    reason="scroll",
                )
            elif name == "computer_use_key":
                return Action(
                    type=ActionType.KEY,
                    key=args.get("key", ""),
                    reason="key press",
                )
            elif name == "computer_use_move_mouse":
                coord = args.get("coordinate", [640, 400])
                return Action(
                    type=ActionType.MOVE_MOUSE,
                    x=int(coord[0]) if coord else 640,
                    y=int(coord[1]) if coord else 400,
                    reason="move mouse",
                )
            elif name == "computer_use_screenshot":
                return Action(type=ActionType.SCREENSHOT, reason="take screenshot")
            elif name == "computer_use_navigate":
                return Action(
                    type=ActionType.NAVIGATE,
                    url=args.get("url", ""),
                    reason="navigate",
                )
            elif name == "computer_use_drag":
                start = args.get("startCoordinate", [0, 0])
                end = args.get("endCoordinate", [0, 0])
                return Action(
                    type=ActionType.DRAG,
                    start_coordinate=start,
                    coordinate=end,
                    reason="drag",
                )
            else:
                # Unknown function call — treat as wait
                return Action(type=ActionType.WAIT, reason=f"unknown action: {name}")

    # No function call — check for text response indicating completion or failure
    for part in content.parts:
        if hasattr(part, 'text') and part.text:
            text_lower = part.text.lower()
            if any(w in text_lower for w in ["cannot", "unable", "blocked", "captcha", "login required"]):
                return Action(type=ActionType.FAIL, reason=part.text[:200])
            if any(w in text_lower for w in ["completed", "accomplished", "done", "finished"]):
                return Action(type=ActionType.DONE, reason=part.text[:200])

    # Default: wait and retry
    return Action(type=ActionType.WAIT, reason="waiting for model direction")


# ── Browser Actions ───────────────────────────────────────────────────────────

async def take_screenshot(page: Page) -> str:
    """Take a screenshot and return as base64 PNG."""
    await page.set_viewport_size({"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT})
    screenshot_bytes = await page.screenshot(type="png", full_page=False)
    return base64.b64encode(screenshot_bytes).decode()


async def _wait_for_navigation(page: Page, timeout_ms: int = 3000) -> None:
    """SPA-safe post-click wait — domcontentloaded + fallback sleep."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        await asyncio.sleep(0.5)


async def execute_action(page: Page, action: Action) -> tuple[bool, str]:
    """Execute a browser action. Returns (success, message)."""
    try:
        if action.type == ActionType.CLICK:
            await page.mouse.click(action.x, action.y)
            await _wait_for_navigation(page)
            return True, f"Clicked at ({action.x}, {action.y})"

        elif action.type == ActionType.TYPE:
            await page.keyboard.type(action.text, delay=50)
            return True, f"Typed: {action.text[:50]}"

        elif action.type == ActionType.KEY:
            await page.keyboard.press(action.key)
            await _wait_for_navigation(page)
            return True, f"Key press: {action.key}"

        elif action.type == ActionType.SCROLL:
            x = action.x or 640
            y = action.y or 400
            delta = action.amount if action.direction == "down" else -(action.amount or 300)
            await page.mouse.move(x, y)
            await page.mouse.wheel(0, delta)
            await asyncio.sleep(0.5)
            return True, f"Scrolled {action.direction} {abs(delta)}px at ({x},{y})"

        elif action.type == ActionType.NAVIGATE:
            await page.goto(action.url, wait_until="domcontentloaded", timeout=30000)
            return True, f"Navigated to {action.url}"

        elif action.type == ActionType.MOVE_MOUSE:
            await page.mouse.move(action.x, action.y)
            return True, f"Moved mouse to ({action.x}, {action.y})"

        elif action.type == ActionType.DRAG:
            start = action.start_coordinate or [0, 0]
            end = action.coordinate or [0, 0]
            await page.mouse.move(start[0], start[1])
            await page.mouse.down()
            await page.mouse.move(end[0], end[1])
            await page.mouse.up()
            return True, f"Dragged from {start} to {end}"

        elif action.type == ActionType.SCREENSHOT:
            return True, "Screenshot taken"

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
    Core agent loop using Gemini 2.5 Computer Use model.
    Screenshot → Gemini Computer Use → Action → Execute → Repeat.
    Yields StepResult for each step (for SSE streaming).
    """
    client = genai.Client(api_key=api_key)
    conversation: list = []

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
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)

        history: list[dict] = []

        for step in range(1, MAX_STEPS + 1):
            # Honour stop signal before each step
            if stop_event is not None and stop_event.is_set():
                break

            t0 = time.time()

            # 1. Screenshot
            screenshot_b64 = await take_screenshot(page)

            # 2. Gemini Computer Use → action
            action, conversation = await get_next_action(
                client, screenshot_b64, goal, history, conversation
            )

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
