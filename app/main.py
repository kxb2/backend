import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.canvases.router import router as canvases_router
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.exports.router import router as exports_router
from app.generations.router import router as generations_router
from app.generations.service import recover_stuck_generations
from app.regenerations.router import router as regenerations_router
from app.regenerations.service import recover_stuck_regenerations
from app.routers import health
from app.storyboards.router import router as storyboards_router

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        Base.metadata.create_all(bind=engine)

        db = SessionLocal()
        try:
            recovered = recover_stuck_generations(db)
            if recovered:
                logger.warning("서버 재시작으로 중단된 생성 job %d개를 failed 처리했습니다.", recovered)

            recovered_regenerations = recover_stuck_regenerations(db)
            if recovered_regenerations:
                logger.warning(
                    "서버 재시작으로 중단된 재생성 job %d개를 failed 처리했습니다.", recovered_regenerations
                )
        finally:
            db.close()
    except Exception:
        logger.warning("DB 연결 실패로 시작 작업을 건너뜁니다 (Supabase 일시정지 여부 확인 필요)", exc_info=True)
    yield


app = FastAPI(title="kx-backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(storyboards_router)
app.include_router(generations_router)
app.include_router(exports_router)
app.include_router(canvases_router)
app.include_router(regenerations_router)
