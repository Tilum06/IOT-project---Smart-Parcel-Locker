"""One PC application. No database writes occur at import time."""
import os
import sqlite3
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .api import create_router
from .db import Database
from .state import BusinessError


def create_app(database: Database | None = None) -> FastAPI:
    db = database or Database(os.getenv('LOCKER_DB_PATH', 'backend/data/locker.sqlite3'))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.initialize()
        # Liveness never survives process restart. Do not replay pending commands.
        with db.write() as conn:
            conn.execute('UPDATE lockers SET online=0')
        yield

    app = FastAPI(title='Smart Parcel Locker Phase 1', version='1.0.0', lifespan=lifespan)
    app.state.db = db

    @app.exception_handler(BusinessError)
    async def business_error(request: Request, exc: BusinessError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={'detail': exc.code})

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return JSONResponse(status_code=409, content={'detail': 'resource_conflict'})

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        return JSONResponse(status_code=503, content={'detail': 'database_unavailable'})

    app.include_router(create_router(db))
    return app


app = create_app()
