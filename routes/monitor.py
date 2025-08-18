from fastapi import APIRouter
from app.services.screen_monitor import get_foreground_app_info

router = APIRouter()

@router.get("/foreground_app")
def read_foreground_app():
    app_name, title = get_foreground_app_info()
    return {"app": app_name, "title": title}
