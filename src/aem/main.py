from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .collectors import ALL_COLLECTORS
from .config import Settings, load_app_config
from .db import init_db, make_engine, make_session_factory
from .scheduler import build_scheduler
from .web.routes import router as web_router

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("aem")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    cfg = load_app_config(settings.config_file)
    engine = make_engine(settings.db_path)
    init_db(engine)

    app.state.settings = settings
    app.state.cfg = cfg
    app.state.session_factory = make_session_factory(engine)
    app.state.collectors = [
        cls() for cid, cls in ALL_COLLECTORS.items()
        if cfg.collectors.get(cid) is None or cfg.collectors[cid].enabled
    ]
    app.state.scheduler = None
    if settings.scheduler_enabled:
        app.state.scheduler = build_scheduler(
            app.state.session_factory, app.state.collectors, settings, cfg)
        app.state.scheduler.start()
        log.info("scheduler started (poll at :%02d, digest at %02d:00 %s)",
                 cfg.poll_minute, cfg.digest_hour, cfg.timezone)
    yield
    if app.state.scheduler:
        app.state.scheduler.shutdown(wait=False)


app = FastAPI(title="Austin Entertainment Monitor", lifespan=lifespan)
app.include_router(api_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
          name="static")
