from dataclasses import dataclass


VERSION = "0.1.0"
APP_NAME = "MetaLens"
SIDECAR_HOST = "127.0.0.1"


@dataclass
class Settings:
    version: str = VERSION
    app_name: str = APP_NAME
    host: str = SIDECAR_HOST
    debug: bool = False


settings = Settings()
