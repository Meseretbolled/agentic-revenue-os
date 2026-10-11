from fastapi import FastAPI

from revos.modes import integration_modes

app = FastAPI(title="Agentic Revenue OS")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
def status() -> dict[str, list[dict[str, str]]]:
    """Which integrations run as REAL and which as MOCK. Never a key or a value."""
    return {"integrations": [{"service": m.service, "mode": m.mode, "why": m.why}
                             for m in integration_modes()]}
