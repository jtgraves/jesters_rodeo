from __future__ import annotations

from fastapi import FastAPI
from mangum import Mangum

from app.routes import public

app = FastAPI(title="Jester's Rodeo")
app.include_router(public.router)

handler = Mangum(app)
