import json
import logging
import sys

from fastapi.logger import logger as _logger

from .app_config import app_config

logger: logging.Logger = _logger


class JsonFormatter(logging.Formatter):
    """
    FR-27: Formats log records as JSON lines for structured log ingestion.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


handler = logging.StreamHandler(sys.stdout)
if app_config.json_logging:
    handler.setFormatter(JsonFormatter())
else:
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
logger.addHandler(handler)
logger.setLevel(app_config.log_level)


def format_exception(exception: Exception):
    return logger.error(f"{type(exception).__name__}:{exception}")
