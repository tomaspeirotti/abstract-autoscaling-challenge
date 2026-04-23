import hashlib
import time

from fastapi import FastAPI, Query

app = FastAPI(title="Challenge API")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/work")
def work(iterations: int = Query(default=10_000, ge=1, le=10_000_000)):
    """CPU-intensive endpoint: iterative SHA-256 hashing."""
    start = time.monotonic()
    data = b"seed"
    for _ in range(iterations):
        data = hashlib.sha256(data).digest()
    elapsed_ms = (time.monotonic() - start) * 1000
    return {"iterations": iterations, "elapsed_ms": round(elapsed_ms, 2)}
