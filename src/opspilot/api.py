import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from opspilot.config import Settings
from opspilot.engine import Engine
from opspilot.models import ApprovalRequest, StartRequest
from opspilot.store import Conflict


def create_app(settings=None):
    settings = settings or Settings()
    engine = Engine(settings)
    pool = ThreadPoolExecutor(max_workers=2)

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True)
        engine.close()

    app = FastAPI(title="OpsPilot", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    def auth(x_api_key: Annotated[str | None, Header()] = None):
        if settings.api_key and not secrets.compare_digest(
            x_api_key or "", settings.api_key.get_secret_value()
        ):
            raise HTTPException(401, "Supply X-API-Key")

    protected = [Depends(auth)]

    def get(iid):
        try:
            return engine.store.get(iid)
        except KeyError:
            raise HTTPException(404, "Unknown incident") from None

    @app.get("/health")
    def health():
        return {"status": "ok", "provider": settings.provider, "actions": "mock_only"}

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(Path(__file__).with_name("index.html"))

    @app.get("/scenarios", dependencies=protected)
    def scenarios():
        from opspilot.tools import DATA

        rows = json.loads((DATA / "observations.json").read_text())
        return [{"id": r["id"], "service": r["service"], "alert": r["alert"]} for r in rows]

    @app.post("/incidents", status_code=202, dependencies=protected)
    def start(
        request: StartRequest, idempotency_key: Annotated[str | None, Header(max_length=200)] = None
    ):
        try:
            iid, created = engine.create(request, idempotency_key)
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if created:
            pool.submit(engine.run, iid)
        return {"id": iid, "created": created, "status_url": f"/incidents/{iid}"}

    @app.get("/incidents/{iid}", dependencies=protected)
    def status(iid: str):
        return get(iid)

    @app.post("/incidents/{iid}/approve", dependencies=protected)
    def approve(iid: str, request: ApprovalRequest):
        get(iid)
        try:
            return engine.run(iid, request.model_dump())
        except Conflict as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/incidents/{iid}/retry", dependencies=protected)
    def retry(iid: str):
        row = get(iid)
        if row["status"] not in {"error", "running"}:
            raise HTTPException(409, "Only failed or interrupted running incidents can be retried")
        return engine.run(iid)

    @app.get("/incidents/{iid}/events", dependencies=protected)
    def events(iid: str, after: Annotated[int, Query(ge=0)] = 0):
        get(iid)

        def stream():
            last = after
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                rows = engine.store.events(iid, last)
                for row in rows:
                    last = row["seq"]
                    yield f"id: {last}\nevent: {row['type']}\ndata: {json.dumps(row['data'])}\n\n"
                if get(iid)["status"] != "running":
                    # Drain the last status event before closing.
                    if not engine.store.events(iid, last):
                        return
                if not rows:
                    yield ": heartbeat\n\n"
                    time.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
