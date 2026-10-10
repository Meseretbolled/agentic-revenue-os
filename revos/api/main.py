from fastapi import FastAPI

app = FastAPI(title="Agentic Revenue OS")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
