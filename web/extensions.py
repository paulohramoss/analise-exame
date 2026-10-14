"""Extensões Flask compartilhadas pelos Blueprints (criadas sem app; ligadas em create_app)."""

import os

from web import security

try:
    from flask_limiter import Limiter
    from flask_limiter.errors import RateLimitExceeded
except ImportError:  # pragma: no cover - dependência instalada em produção via requirements.txt
    Limiter = None
    RateLimitExceeded = None

limiter = None
if Limiter is not None:
    limiter = Limiter(
        key_func=security.rate_limit_key,
        storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
        default_limits=[],
    )


def rate_limit(env_name: str, default: str, methods: list[str] | None = None):
    """Limite por IP lido de `env_name` (formato do flask-limiter, ex.: "10 per hour")."""
    def decorator(fn):
        if limiter is None:
            return fn
        return limiter.limit(os.environ.get(env_name, default), methods=methods)(fn)
    return decorator
