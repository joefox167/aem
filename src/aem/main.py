from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .collectors import ALL_COLLECTORS
from .collectors.base import Collector
from .collectors.ticketmaster import TicketmasterCollector
from .config import AppConfig, Settings, load_app_config
from .db import init_db, make_engine, make_session_factory
from .scheduler import build_scheduler
from .web.routes import router as web_router

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("aem")


def build_collectors(cfg: AppConfig, settings: Settings) -> list[Collector]:
    """Instantiate every enabled collector with its configured options."""
    collectors: list[Collector] = []
    for cid, cls in ALL_COLLECTORS.items():
        ccfg = cfg.collectors.get(cid)
        if ccfg is not None and not ccfg.enabled:
            continue
        options = dict(ccfg.options) if ccfg else {}
        if cid == TicketmasterCollector.id:
            options.setdefault("api_key", settings.ticketmaster_api_key)
            if not options["api_key"]:
                log.warning("collector %s skipped: AEM_TICKETMASTER_API_KEY is not set", cid)
                continue
        collectors.append(cls(options))
    return collectors


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    cfg = load_app_config(settings.config_file)
    engine = make_engine(settings.db_path)
    init_db(engine)

    app.state.settings = settings
    app.state.cfg = cfg
    app.state.session_factory = make_session_factory(engine)
    app.state.collectors = build_collectors(cfg, settings)
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
