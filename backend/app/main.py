"""FastAPI application factory."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


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
