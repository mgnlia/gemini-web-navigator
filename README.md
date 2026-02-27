# 🤖 Gemini Web Navigator

**Universal Web Navigation Agent powered by Gemini 2.0 Flash Vision**

> A browser agent that uses Gemini's multimodal vision to observe browser screenshots and execute user intents — without DOM/API access. Pure vision-driven automation.

Built for the [Gemini Live Agent Challenge](https://geminiliveagentchallenge.devpost.com/) — targeting the **Best UI Navigator** ($10K) and **Grand Prize** ($25K) tracks.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    User Interface (FastAPI)                   │
│  Goal Input ──► POST /run ──► SSE Stream ──► Live Feed      │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    Agent Loop (agent.py)                      │
│                                                               │
│  ┌──────────┐    ┌──────────────────┐    ┌───────────────┐  │
│  │Playwright│───►│  Screenshot PNG  │───►│ Gemini 2.0    │  │
│  │ Browser  │    │  (1280×800)      │    │ Flash Vision  │  │
│  └──────────┘    └──────────────────┘    └───────┬───────┘  │
│       ▲                                          │           │
│       │                                          ▼           │
│       │          ┌──────────────────────────────────────┐   │
│       └──────────│  Action Parser                        │   │
│                  │  click(x,y) │ type(text) │ scroll     │   │
│                  │  navigate(url) │ wait │ done │ fail   │   │
│                  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                 Google Cloud Run                              │
│  Container: Python 3.11 + Playwright Chromium + FastAPI      │
│  Region: us-central1 │ Memory: 2Gi │ CPU: 2                  │
└─────────────────────────────────────────────────────────────┘
```

## How It Works

1. **User provides a goal** — e.g. "Search for climate change on Google and summarize the first result"
2. **Playwright launches Chromium** headlessly and navigates to the start URL
3. **Screenshot captured** at 1280×800 and sent to **Gemini 2.0 Flash** with the goal
4. **Gemini analyzes the screenshot** visually — reads text, identifies buttons/forms/links — and returns a JSON action
5. **Action executed** via Playwright (click at coordinates, type text, scroll, navigate)
6. **Loop repeats** — new screenshot, new Gemini call — until `done` or `fail`
7. **Every step streamed** via SSE to the web UI with live screenshot updates

## Key Features

- 🎯 **Pure vision-driven** — zero DOM access, zero CSS selectors, zero API calls to target sites
- 🔄 **Real-time streaming** — watch the agent work step-by-step with live screenshots
- 🧠 **Gemini 2.0 Flash** — fast multimodal vision for screenshot analysis
- 🌐 **Universal** — works on any website without custom adapters
- ☁️ **Cloud Run** — scalable, serverless deployment on Google Cloud
- 🛑 **Session cancellation** — stop a running agent via `POST /stop/{session_id}`

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Vision AI | Gemini 2.0 Flash (`gemini-2.0-flash-exp`) |
| Browser | Playwright + Chromium |
| Backend | FastAPI + Python 3.11 |
| Streaming | Server-Sent Events (SSE) |
| Hosting | Google Cloud Run |
| Container | Docker |
| Build | Google Cloud Build |
| Package mgr | uv |

## Quick Start

### Local Development

```bash
# Clone
git clone https://github.com/mgnlia/gemini-web-navigator
cd gemini-web-navigator

# Install dependencies
uv sync

# Install Playwright browsers
uv run playwright install chromium

# Set API key
export GEMINI_API_KEY=your_key_here

# Run server
uv run uvicorn main:app --reload --port 8080

# Open http://localhost:8080
```

### CLI Mode

```bash
uv run python agent.py "Search for Gemini AI on Google and click the first result"
```

### Deploy to Cloud Run

The recommended path is via **Cloud Build** — it builds the Docker image, pushes it to
Container Registry, and deploys to Cloud Run in one step.  
`GEMINI_API_KEY` is read from [Secret Manager](https://cloud.google.com/secret-manager) 
(secret name `gemini-api-key`) so it is never stored in source code or build logs.

```bash
# 1. Store your API key in Secret Manager (one-time setup)
echo -n "$GEMINI_API_KEY" | \
  gcloud secrets create gemini-api-key --data-file=- --replication-policy=automatic

# 2. Grant Cloud Build access to the secret
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:$(gcloud projects describe $PROJECT_ID \
      --format='value(projectNumber)')@cloudbuild.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# 3. Submit the build (builds image + deploys to Cloud Run)
export PROJECT_ID=your-gcp-project

gcloud builds submit --config cloudbuild.yaml \
  --project "$PROJECT_ID"
```

For a **manual one-shot deploy** (no Cloud Build), use:

```bash
export PROJECT_ID=your-gcp-project
export GEMINI_API_KEY=your_key_here

# Build & push image
docker build -t gcr.io/$PROJECT_ID/gemini-web-navigator .
docker push gcr.io/$PROJECT_ID/gemini-web-navigator

# Deploy to Cloud Run
gcloud run deploy gemini-web-navigator \
  --image gcr.io/$PROJECT_ID/gemini-web-navigator \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --concurrency 10 \
  --set-env-vars GEMINI_API_KEY=$GEMINI_API_KEY
```

> **`--allow-unauthenticated`** is required so the public UI can reach the service without
> Google credentials.  Pass `--no-allow-unauthenticated` and add IAM bindings if you want
> to restrict access.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | ✅ | Google AI Studio API key — set via `--set-env-vars` or Secret Manager |

## API Reference

### `POST /run`

Run the navigator agent.

**Request:**
```json
{
  "goal": "Search for 'AI agents' on Google",
  "start_url": "https://www.google.com",
  "headless": true,
  "session_id": "my-unique-session-123"
}
```

**Response:** SSE stream of events:
```
data: {"type": "step", "step": 1, "action": "navigate", "message": "...", "screenshot": "<base64>", "elapsed_ms": 1200}
data: {"type": "step", "step": 2, "action": "click", "message": "Clicked at (640, 400)", "screenshot": "<base64>", "elapsed_ms": 800}
data: {"type": "done", "message": "Goal accomplished: Found AI agents article"}
```

### `POST /stop/{session_id}`

Cancel a running agent session after the current step completes.

```bash
curl -X POST http://localhost:8080/stop/my-unique-session-123
# {"status": "stopping", "session_id": "my-unique-session-123"}
```

### `GET /health`

```json
{"status": "ok", "service": "gemini-web-navigator", "version": "1.0.0"}
```

## Example Goals

- `"Search for 'climate change' on Google and click the Wikipedia result"`
- `"Go to news.ycombinator.com and tell me the top 3 stories"`
- `"Navigate to github.com/trending and find the most starred repo this week"`
- `"Go to Wikipedia and find the article about the Eiffel Tower height"`

## Hackathon Submission

- **Challenge:** [Gemini Live Agent Challenge](https://geminiliveagentchallenge.devpost.com/)
- **Track:** Best UI Navigator ($10K)
- **Google Cloud Service:** Cloud Run (hosting) + Cloud Build (CI/CD)
- **Gemini Model:** Gemini 2.0 Flash (`gemini-2.0-flash-exp`)
- **SDK:** Google GenAI Python SDK (`google-genai`)

---

*Built with ❤️ using Gemini 2.0 Flash + Playwright + FastAPI*
