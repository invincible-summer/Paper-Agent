"""Aggregates all v1 routers under /api/v1."""
from fastapi import APIRouter

from app.api.v1 import admin, auth, chat, elements, feedback, health, reader, usage_document

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(chat.router)
api_router.include_router(elements.router)
api_router.include_router(feedback.router)
api_router.include_router(usage_document.router)

api_router.include_router(reader.router)
