"""FastAPI application factory."""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Add project root to sys.path so existing agents/core/tools are importable
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings


def _prewarm_agent_stack(mode: str = "blocking") -> dict:
    """Testable compatibility wrapper for the local, best-effort warmup."""
    from core.prewarm import prewarm_agent_stack
    return prewarm_agent_stack(mode)


def _retire_remote_fulltext_cache() -> dict:
    """Testable wrapper for the one-time legacy cache migration."""
    from core.remote_fulltext_retirement import retire_remote_fulltext
    return retire_remote_fulltext(_PROJECT_ROOT, dry_run=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One-time, idempotent retirement of legacy network-paper full-text
    # derivatives.  It is intentionally best-effort so a corrupt old cache
    # never prevents the API from becoming healthy; uploads and exports are
    # outside the migration's deletion boundary.
    try:
        # Use the process-wide daemon I/O workers rather than asyncio's
        # per-loop default executor.  Lifespan tests and repeated app startup
        # create short-lived event loops; a default-executor job can otherwise
        # make ``asyncio.run`` wait indefinitely during loop shutdown.
        from core.blocking import run_io_bound
        await run_io_bound(_retire_remote_fulltext_cache)
    except Exception:  # noqa: BLE001
        # Keep startup resilient; the CLI remains available for an explicit
        # retry/audit and logs can be inspected by operators.
        import logging
        logging.getLogger(__name__).exception("remote full-text retirement migration failed")
    # Runtime policy is stored in users.db.  Warmup is local-only and best
    # effort; failure must never prevent the HTTP server from becoming ready.
    background_task = None
    try:
        from core.runtime_performance_policy import get_performance_policy
        policy = get_performance_policy()
        mode = policy.startup_prewarm_mode
        if mode == "blocking":
            from core.blocking import run_cpu_bound
            await run_cpu_bound(_prewarm_agent_stack, mode)
        elif mode == "background":
            from core.blocking import run_cpu_bound
            background_task = asyncio.create_task(
                run_cpu_bound(_prewarm_agent_stack, mode))
        else:
            from core.prewarm import _set
            _set(active_mode=mode, prewarm_state="disabled")
        yield
    finally:
        if background_task is not None and not background_task.done():
            background_task.cancel()
        try:
            if background_task is not None:
                await background_task
        except (asyncio.CancelledError, Exception):
            pass
        from tools.search.http_client import close_search_http_clients
        await close_search_http_clients()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        # D-089: explicit origin allow-list (never "*"). The backend binds to
        # 0.0.0.0, so a wildcard policy with credentials would let any origin
        # drive authenticated requests (notably /v1/chat/completions). The list
        # is derived from frontend_origin + localhost/WSL loopback variants,
        # and extras via CORS_ORIGINS for a deployed domain.
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from app.api.v1.router import api_router
    app.include_router(api_router)

    # OpenAI-compatible endpoints at /v1 (for QingXiaoDa marketplace, DESIGN D-064)
    from app.api.v1.openai_compat import router as openai_router
    app.include_router(openai_router)

    # Generated artifact downloads (x_soda.attachments targets).
    from app.api.v1.files import router as files_router
    app.include_router(files_router)

    # Root-level /health alias: deploy platforms (Render/K8s liveness, etc.)
    # probe /health by default, while the canonical route lives at /api/v1/health.
    from app.api.v1.health import health as _health
    app.get("/health", tags=["health"], include_in_schema=False)(_health)
    return app


app = create_app()
