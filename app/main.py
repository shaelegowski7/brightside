"""FastAPI entrypoint — mirrors sentimentfx-backend's structure: routes and
scheduler startup live in main.py, DB session handling in database.py.
Schema is Alembic-managed (`alembic upgrade head` runs as the Railway
release step — see Procfile), not create_all(), per this project's stack."""
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import auth, monitoring, purchases, scan, schemas
from .config import get_config, get_settings
from .database import engine, get_db
from .decision.engine import DecisionConfig
from .pricing import fees
from .scheduler import scheduler, start_scheduler

app = FastAPI(title="FBA Deal Scanner")

# Only enabled if PWA_ORIGIN is set (Phase 2 /scan, called cross-origin
# from a separately-deployed PWA) -- empty means CORS is off entirely, not
# "allow everything".
_pwa_origin = get_settings().pwa_origin
if _pwa_origin:
    app.add_middleware(
        CORSMiddleware, allow_origins=[_pwa_origin], allow_methods=["POST"],
        allow_headers=["*"], allow_credentials=False,
    )

start_scheduler()


@app.get("/")
def root():
    return {"message": "FBA Deal Scanner"}


@app.get("/status/summary")
def status_summary(hours: int = 24, db: Session = Depends(get_db)):
    return monitoring.build_summary(db, hours=hours)


@app.post("/purchases")
def create_purchase(
    body: schemas.PurchaseCreate, db: Session = Depends(get_db), _: None = Depends(auth.require_shared_secret)
):
    try:
        purchase = purchases.log_purchase(db, body.asin, body.qty, body.actual_buy_price, body.notes)
    except purchases.NoScoreFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "id": purchase.id, "score_id": purchase.score_id, "qty": purchase.qty,
        "actual_buy_price": purchase.actual_buy_price, "notes": purchase.notes, "ts": purchase.ts,
    }


@app.post("/outcomes")
def create_outcome(
    body: schemas.OutcomeCreate, db: Session = Depends(get_db), _: None = Depends(auth.require_shared_secret)
):
    try:
        outcome = purchases.log_outcome(
            db, body.purchase_id, body.sold_price, body.sold_date, body.actual_fees, body.notes
        )
    except purchases.PurchaseNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except purchases.OutcomeAlreadyExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "purchase_id": outcome.purchase_id, "sold_price": outcome.sold_price, "sold_date": outcome.sold_date,
        "actual_fees": outcome.actual_fees, "notes": outcome.notes,
    }


@app.post("/scan", response_model=schemas.ScanResponse)
def create_scan(
    body: schemas.ScanRequest, db: Session = Depends(get_db), _: None = Depends(auth.require_shared_secret)
):
    app_cfg = get_config()
    decision_cfg = DecisionConfig.from_app_config(app_cfg)
    fee_provider = fees.build_fee_provider(db, app_cfg)
    return scan.run_scan(db, body.ean, body.buy_price, decision_cfg, fee_provider, app_cfg)


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
