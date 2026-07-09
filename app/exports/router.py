# TODO: POST /storyboards/{id}/exports/pdf, POST /storyboards/{id}/exports/image, GET /exports/{id}
from fastapi import APIRouter

router = APIRouter(prefix="/exports", tags=["exports"])