# Prism UI

The Prism web front end: a chat-driven analytics workspace (Next.js App Router, TypeScript, Tailwind).
It talks to the agent over HTTP and Server-Sent Events.

## Run

```bash
make frontend            # or: scripts/start_frontend.sh
```

Serves http://localhost:3000 (the only origin the agent allows). The script checks Node >= 20, installs
dependencies when needed, and warns if the agent is not reachable.

## Configuration

`NEXT_PUBLIC_AGENT_URL` sets the agent URL (default `http://localhost:8000`); see `.env.example`.

## Tests

```bash
make test-ui             # lint + typecheck + unit tests
make e2e                 # Playwright smoke against a mocked agent
```
