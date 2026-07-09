# TODO: POST /canvases, GET /canvases/{id}, PUT /canvases/{id}
from fastapi import APIRouter

router = APIRouter(prefix="/canvases", tags=["canvases"])