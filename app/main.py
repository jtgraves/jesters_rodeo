from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mangum import Mangum

from app.routes import admin, auth_routes, public, webhooks

app = FastAPI(title="Jester's Rodeo")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(public.router)
app.include_router(webhooks.router)
app.include_router(auth_routes.router)
app.include_router(admin.router)

handler = Mangum(app)
