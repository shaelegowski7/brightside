"""FastAPI entrypoint — mirrors sentimentfx-backend's structure: routes and
scheduler startup live in main.py, DB session handling in database.py.
Schema is Alembic-managed (`alembic upgrade head` runs as the Railway
release step — see Procfile), not create_all(), per this project's stack."""
from fastapi import FastAPI
from sqlalchemy import text

from .database import engine
from .scheduler import scheduler, start_scheduler

app = FastAPI(title="FBA Deal Scanner")

start_scheduler()


@app.get("/")
def root():
    return {"message": "FBA Deal Scanner"}


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        print(f"[HEALTH] db check failed: {e}")
        db_ok = False
    status = "ok" if db_ok and scheduler.running else "degraded"
    return {
        "status": status,
        "checks": {
            "db": "ok" if db_ok else "error",
            "scheduler": "ok" if scheduler.running else "not_running",
        },
    }
