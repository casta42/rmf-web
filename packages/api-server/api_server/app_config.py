import importlib.util
import os
import sys
import urllib.parse
from dataclasses import dataclass
from importlib.abc import Loader
from typing import List, Optional, cast


@dataclass
class AppConfig:
    host: str
    port: int
    db_url: str
    public_url: urllib.parse.ParseResult
    cache_directory: str
    log_level: str
    builtin_admin: str
    jwt_public_key: Optional[str]
    oidc_url: Optional[str]
    aud: str
    iss: Optional[str]
    ros_args: List[str]
    # FR-27: when True, logs are emitted as JSON lines instead of plain text.
    json_logging: bool = False
    # FR-17: battery fraction (0.0 depleted - 1.0 full) below which a low
    # battery alert is created.
    low_battery_threshold: float = 0.15
    # FR-17: seconds a robot may stay stationary while executing a task before
    # a stuck alert is created.
    stuck_timeout: float = 60
    # DR-3/FR-10: path to the site's zones.yaml served read-only at /zones
    # for the dashboard map overlays. None disables the route (404).
    zones_file: Optional[str] = None

    def __post_init__(self):
        self.public_url = urllib.parse.urlparse(cast(str, self.public_url))

    def get_tortoise_orm_config(self):
        tortoise_config = {}
        tortoise_config["connections"] = {"default": self.db_url}
        tortoise_config["apps"] = {
            "models": {
                "models": ["api_server.models.tortoise_models", "aerich.models"],
                "default_connection": "default",
            },
        }
        return tortoise_config


def load_config(config_file: str) -> AppConfig:
    spec = importlib.util.spec_from_file_location("config", config_file)
    if spec is None:
        raise FileNotFoundError(f"Could not find config file '{config_file}'")
    module = importlib.util.module_from_spec(spec)
    loader = spec.loader
    if not isinstance(loader, Loader):
        raise RuntimeError("unable to load module")
    sys.path.append(os.path.dirname(config_file))
    loader.exec_module(module)
    config = AppConfig(**module.config)
    if "RMF_API_SERVER_LOG_LEVEL" in os.environ:
        config.log_level = os.environ["RMF_API_SERVER_LOG_LEVEL"]
    return config


app_config = load_config(
    os.environ.get(
        "RMF_API_SERVER_CONFIG",
        f"{os.path.dirname(__file__)}/default_config.py",
    )
)
