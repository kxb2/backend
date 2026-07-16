# TODO: POST /canvases, GET /canvases/{id}, PUT /canvases/{id}
from fastapi import APIRouter

# GET /canvases/{id} 응답을 설계할 때,
# Cut/Storyboard를 조인해서 image_url·prompt_text를 이미 채운 상태로 내려주기

router = APIRouter(prefix="/canvases", tags=["canvases"])
