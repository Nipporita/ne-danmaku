"""WSGI/ASGI entrypoint."""

from nekocast_danmaku.app import create_app
from nekocast_danmaku.config import load_config
import uvicorn


app = create_app(load_config())

uvicorn.run(app)