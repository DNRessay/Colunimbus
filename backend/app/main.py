import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum

from .config import settings
from .db import init_db
from .invest.router import router as invest_router
from .routers import auth, bridge, core, erp_invoices, erpnext, imports, reconciliation

logging.getLogger().setLevel(logging.INFO)

if settings.auto_create_tables:
    init_db()

app = FastAPI(title="C.T.H.A.I API", redirect_slashes=False, docs_url="/docs" if settings.debug else None,
              redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def strip_trailing_slash(request: Request, call_next):
    # Old clients call /api/foo/ ; routes are registered without the slash. Rewriting
    # instead of redirecting keeps cross-origin fetch() working.
    path = request.scope["path"]
    if len(path) > 1 and path.endswith("/"):
        request.scope["path"] = path.rstrip("/")
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = {}
    for e in exc.errors():
        field = ".".join(str(p) for p in e["loc"] if p not in ("body", "query", "path", "form"))
        errors.setdefault(field or "non_field_errors", []).append(e["msg"])
    return JSONResponse({"detail": errors}, status_code=400)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    logging.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Internal server error."}, status_code=500)


for r in (auth.router, auth.practice_router, auth.social_router, core.router, bridge.router, erpnext.router,
          imports.router, imports.callback_router, reconciliation.router, erp_invoices.router, invest_router):
    app.include_router(r)


@app.get("/")
def root():
    return {"name": "C.T.H.A.I API", "health": "/api/health"}


# Lambda entrypoint: app.main.handler
handler = Mangum(app, lifespan="off")
