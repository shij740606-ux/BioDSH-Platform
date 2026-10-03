import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from biodsh_platform import __version__
from biodsh_platform.api import api_router, health_router
from biodsh_platform.config import get_settings
from biodsh_platform.db import engine
from biodsh_platform.feishu import router as feishu_router
from biodsh_platform.invitations import landing as invitation_landing
from biodsh_platform.invitations import router as invitation_router
from biodsh_platform.models import Base
from biodsh_platform.notifications import run as notify_run


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().auto_create_schema:
        Base.metadata.create_all(bind=engine)
    stop = threading.Event()
    thread = threading.Thread(target=notify_run, args=(stop,), daemon=True)
    if get_settings().feishu_notifications_enabled:
        thread.start()
    try:
        yield
    finally:
        stop.set()


def create_app() -> FastAPI:
    settings = get_settings()
    docs_url = "/docs" if settings.docs_enabled else None
    redoc_url = "/redoc" if settings.docs_enabled else None
    openapi_url = "/openapi.json" if settings.docs_enabled else None
    app = FastAPI(
        title=settings.service_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(feishu_router, prefix=settings.api_prefix)
    app.include_router(invitation_router, prefix=settings.api_prefix)
    app.add_api_route("/join", invitation_landing)
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
