"""
Cache em memória com expiração (TTL) e tamanho máximo, seguro entre threads.

Substitui os dicts soltos usados como cache pelo app: eles só cresciam (uma
entrada por análise, por domínio de e-mail, por PDF) e nunca liberavam memória
num processo de vida longa como o worker. Aqui, ao passar de `maxsize`, sai a
entrada usada há mais tempo (LRU).
"""

import threading
import time
from collections import OrderedDict
from typing import Any

_MISSING = object()


class TTLCache:
    def __init__(self, maxsize: int, ttl_seconds: float):
        if maxsize <= 0:
            raise ValueError("maxsize deve ser positivo")
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            entry = self._data.get(key, _MISSING)
            if entry is _MISSING:
                return default
            expires_at, value = entry
            if expires_at <= time.monotonic():
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return value

    def set(self, key, value, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def pop(self, key, default=None):
        with self._lock:
            entry = self._data.pop(key, _MISSING)
        return default if entry is _MISSING else entry[1]

    def __contains__(self, key) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
