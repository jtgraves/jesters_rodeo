from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mangum import Mangum

from app.routes import admin, auth_routes, public, webhooks

app = FastAPI(title="Jesters' Reaux-de-Eaux")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(public.router)
app.include_router(webhooks.router)
app.include_router(auth_routes.router)
app.include_router(admin.member_router)
app.include_router(admin.router)

_mangum_handler = Mangum(app)


def handler(event, context):
    # A direct EventBridge invoke from the warming schedule
    # (infra/jesters_rodeo_stack.py's AppWarmingScheduleRule) -- not an API
    # Gateway request, so it has none of the shape Mangum expects. Returning
    # before Mangum ever sees it is what keeps this a trivial no-op: the
    # whole point is to keep an execution environment warm without doing
    # real work (or real GB-seconds) on every ping.
    if event.get("warmer"):
        return {"statusCode": 200, "body": "warm"}
    return _mangum_handler(event, context)
