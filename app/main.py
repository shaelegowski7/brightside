"""FastAPI entrypoint — mirrors sentimentfx-backend's structure: routes and
scheduler startup live in main.py, DB session handling in database.py.
Schema is Alembic-managed (`alembic upgrade head` runs as the Railway
release step — see Procfile), not create_all(), per this project's stack."""
import asyncio
import hmac

from fastapi import Cookie, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import auth, auth_monitor, crawl_runner, dashboard, monitoring, purchases, scan, schemas
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
        CORSMiddleware, allow_origins=[_pwa_origin], allow_methods=["POST", "GET"],
        allow_headers=["*"], allow_credentials=False,
    )

start_scheduler()


@app.get("/")
def root():
    return {"message": "FBA Deal Scanner"}


@app.get("/status/summary")
def status_summary(hours: int = 24, db: Session = Depends(get_db)):
    return monitoring.build_summary(db, hours=hours)


DEALS_COOKIE = "deals_secret"


@app.get("/deals", response_class=HTMLResponse)
def deals_dashboard(db: Session = Depends(get_db), deals_secret: str | None = Cookie(default=None)):
    expected = get_settings().pwa_shared_secret
    if not expected or deals_secret != expected:
        return dashboard.render_login_page()
    rows = dashboard.get_confirmed_deals(db)
    return dashboard.render_page(rows)


@app.post("/deals/login")
def deals_login(body: schemas.DealsLogin):
    expected = get_settings().pwa_shared_secret
    if not expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not body.secret or not hmac.compare_digest(body.secret, expected):
        auth_monitor.record_failed_attempt("deals_login")
        raise HTTPException(status_code=401, detail="unauthorized")
    resp = JSONResponse({"ok": True})
    # Cookie holds the secret itself -- no session store, matching this
    # app's single-shared-secret model (see auth.py). httponly blocks JS
    # access; secure is relaxed only in development so local http testing
    # (no TLS) still works.
    resp.set_cookie(
        key=DEALS_COOKIE,
        value=body.secret,
        httponly=True,
        samesite="lax",
        secure=get_settings().environment != "development",
        max_age=60 * 60 * 24 * 30,
    )
    return resp


@app.get("/deals.json", response_model=list[schemas.ConfirmedDeal])
def deals_json(db: Session = Depends(get_db), _: None = Depends(auth.require_shared_secret)):
    """Same query as /deals, as JSON -- feeds the PWA's confirmed-deals
    view, which already sends X-Shared-Secret (see pwa/src/api.ts)."""
    rows = dashboard.get_confirmed_deals(db)
    return [
        schemas.ConfirmedDeal(
            title=r.title,
            retailer=r.retailer,
            retailer_url=r.retailer_url,
            asin=r.asin,
            match_confidence=r.match_confidence,
            buy_price_pence=r.buy_price_pence,
            sell_price_pence=r.sell_price_pence,
            net_profit_pence=r.net_profit_pence,
            roi=r.roi,
            est_monthly_sales=r.est_monthly_sales,
            verdict=r.verdict,
            flags=r.flags,
            ts=r.ts,
            amazon_url=f"https://www.amazon.co.uk/dp/{r.asin}" if r.asin else None,
        )
        for r in rows
    ]


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


@app.post("/crawl")
def trigger_crawl(_: None = Depends(auth.require_shared_secret)):
    started, status = crawl_runner.start_crawl()
    return {"started": started, **status}


@app.get("/crawl/status")
def crawl_status(_: None = Depends(auth.require_shared_secret)):
    return crawl_runner.get_status()


_CRAWL_WS_ACTIVE_POLL_INTERVAL_S = 0.3
_CRAWL_WS_IDLE_POLL_INTERVAL_S = 3.0
_CRAWL_WS_AUTH_TIMEOUT_S = 10.0


@app.websocket("/crawl/ws")
async def crawl_ws(websocket: WebSocket):
    """Live crawl status, replacing the PWA's old fixed-interval REST
    polling. Auth can't use the X-Shared-Secret header /crawl and
    /crawl.json use -- the browser's native WebSocket API has no way to set
    custom headers on the handshake -- and a ?secret= query param would put
    it in server/proxy logs and browser history, exactly what auth.py's
    header-not-query-param header choice exists to avoid. So the secret is
    sent as the first text message after connecting instead, checked before
    anything else happens.

    Pushes crawl_runner's in-memory state on a short internal poll (not
    true pub/sub off the background crawl thread -- that would need
    asyncio.run_coroutine_threadsafe bridging from a plain thread, real
    complexity for a 3-user tool) and only sends when it actually changed,
    so the client gets near-real-time updates without the server hammering
    itself or the client re-requesting on its own timer. Polls at the fast
    interval while a crawl is actually running and backs off to the slow
    one otherwise -- the Green Deals tab stays connected for as long as it's
    open (see pwa/src/components/CrawlPanel.tsx), which without this would
    mean polling every 300ms indefinitely even with nothing happening."""
    await websocket.accept()
    try:
        secret = await asyncio.wait_for(websocket.receive_text(), timeout=_CRAWL_WS_AUTH_TIMEOUT_S)
    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="no secret received in time")
        return
    except WebSocketDisconnect:
        return

    expected = get_settings().pwa_shared_secret
    if not expected:
        await websocket.close(code=4001, reason="unauthorized")
        return
    if not secret or not hmac.compare_digest(secret, expected):
        auth_monitor.record_failed_attempt("crawl_ws")
        await websocket.close(code=4001, reason="unauthorized")
        return

    last_sent: dict | None = None
    try:
        while True:
            status = crawl_runner.get_status()
            if status != last_sent:
                await websocket.send_json(status)
                last_sent = status
            poll_interval = _CRAWL_WS_ACTIVE_POLL_INTERVAL_S if status["running"] else _CRAWL_WS_IDLE_POLL_INTERVAL_S
            await asyncio.sleep(poll_interval)
    except WebSocketDisconnect:
        pass


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
