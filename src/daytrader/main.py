import uvicorn

from .api import create_app
from .config import Settings


def run() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.app_host, port=settings.app_port)


app = create_app()
