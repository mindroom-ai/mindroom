# Frontend And Platform Live Workflows

Use this reference for the bundled dashboard, the frontend-only dev server, screenshot capture, and the SaaS platform sandbox.

## Core Dashboard: Bundled With The Backend

The default backend serves the dashboard on `http://localhost:8765`.

Use this when you want the real app with the real backend instead of a frontend-only dev server.

Quick checks:

```bash
curl -s http://localhost:8765/api/health
curl -I http://localhost:8765
```

## Core Frontend: Dev Server Mode

Use this when iterating on the React app itself.

```bash
./run-frontend.sh
```

That starts Vite on port `3003`.

Equivalent manual path:

```bash
cd frontend
bun install
bun run dev -- --host 0.0.0.0 --port 3003
```

## Core Frontend Screenshots

Preferred wrapper:

```bash
python frontend/take_screenshot.py
python frontend/take_screenshot.py 3003
python frontend/take_screenshot.py http://localhost:3003
```

Direct Puppeteer path:

```bash
cd frontend
DEMO_URL="http://localhost:8765" bun run screenshot
DEMO_URL="http://localhost:3003" bun run screenshot
```

Outputs land in `frontend/screenshots/`.
The script captures the full page, an agent-selected view, and the Models tab.

If the session supports local image viewing, open the generated PNGs directly after capture instead of describing them blindly.

## Interact With The Frontend

Prefer the real UI over assumptions.

- Use the bundled dashboard at `http://localhost:8765` for backend-connected checks.
- Use `http://localhost:3003` for React-only UI iteration.
- Use screenshot capture for deterministic visual evidence.
- If browser automation is available in the session, drive the live URL directly.
- If browser automation is not available, combine screenshots with `curl` and backend API checks.

## SaaS Platform Sandbox

Start the full local Compose sandbox:

```bash
just local-platform-compose-up
```

Stop it:

```bash
just local-platform-compose-down
```

Tail logs:

```bash
just local-platform-compose-logs
```

For service-by-service development instead of Compose:

```bash
cd saas-platform/platform-backend
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

```bash
cd saas-platform/platform-frontend
bun install
bun run dev
```

The platform frontend dev server runs on `http://localhost:3000` with `NEXT_PUBLIC_DEV_AUTH=true`.

## Platform Frontend Screenshots

Use the built-in screenshot script.
It will start the dev server automatically if needed and save PNGs under `saas-platform/platform-frontend/screenshots/`.

```bash
cd saas-platform/platform-frontend
bun run screenshot
```

Set `PORT` if the frontend is running somewhere else.

```bash
cd saas-platform/platform-frontend
PORT=3001 bun run screenshot
```

The current script captures:

- Landing page desktop
- Landing page mobile
- Login page
- Signup page
