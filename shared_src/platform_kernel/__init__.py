"""Platform-level shared kernel for service bootstrap and config."""

from .config_base import BaseServiceConfig
from .fastapi_kernel import (
    error_payload,
    create_request_id_middleware,
    create_http_exception_handler,
    create_validation_exception_handler,
    create_unhandled_exception_handler,
    install_common_fastapi_handlers,
    install_cors,
)

__all__ = [
    "BaseServiceConfig",
    "error_payload",
    "create_request_id_middleware",
    "create_http_exception_handler",
    "create_validation_exception_handler",
    "create_unhandled_exception_handler",
    "install_common_fastapi_handlers",
    "install_cors",
]
