# TODO: POST /storyboards/{id}/cuts/{cutId}/regeneration, GET /regenerations/{id}
from fastapi import APIRouter

router = APIRouter(prefix="/regenerations", tags=["regenerations"])