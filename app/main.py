from __future__ import annotations

from fastapi import FastAPI
from mangum import Mangum

from app.routes import public, webhooks

app = FastAPI(title="Jester's Rodeo")
app.include_router(public.router)
app.include_router(webhooks.router)

handler = Mangum(app)
