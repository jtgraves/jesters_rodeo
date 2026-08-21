from __future__ import annotations

from fastapi import FastAPI
from mangum import Mangum

from app.routes import auth_routes, public, webhooks

app = FastAPI(title="Jester's Rodeo")
app.include_router(public.router)
app.include_router(webhooks.router)
app.include_router(auth_routes.router)

handler = Mangum(app)
