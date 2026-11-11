from __future__ import annotations

import atexit
import asyncio
import hashlib
import logging
import os
import threading
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Any

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def env_int(name: str, default: int, minimum: int = 1, maximum: int = 1_000_000) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, str(default)).strip().lower()
    if raw not in {"true", "false", "1", "0", "yes", "no"}:
        raise RuntimeError(f"{name} must be true or false")
    return raw in {"true", "1", "yes"}


class CapacityError(Exception):
    """A bounded pool cannot admit more work within the checkout budget."""


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = Counter()

    def add(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[key] += amount

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._counts)


METRICS = Metrics()


@dataclass
class _Idle:
    connection: Any
    idle_since: float
    created_at: float


class BoundedPool:
    """Exclusive checkouts, with limits on OPEN connections, not just idle ones.

    Network open/close never holds the global pool lock. Failed opens relinquish
    reservations. Idle sessions can be evicted to make room for another database.
    A connection is never shared by concurrent requests. Idle expiry is lazy.
    """
    def __init__(self, max_total=16, max_per_key=4, max_idle_per_key=2,
                 wait_seconds=0.1, idle_seconds=120.0, lifetime_seconds=1800.0):
        if min(max_total, max_per_key) < 1 or max_idle_per_key < 0:
            raise ValueError("Invalid pool limits")
        self.max_total = max_total
        self.max_per_key = min(max_per_key, max_total)
        self.max_idle_per_key = min(max_idle_per_key, self.max_per_key)
        self.wait_seconds = max(0.0, wait_seconds)
        self.idle_seconds = idle_seconds
        self.lifetime_seconds = lifetime_seconds
        self._cv = threading.Condition()
        self._idle = {}
        self._open = Counter()
        self._busy = 0
        self._closed = False
        self._pid = os.getpid()
        self.high_watermark = 0

    def _drop_locked(self, key):
        self._open[key] -= 1
        if not self._open[key]:
            del self._open[key]
        if key in self._idle and not self._idle[key]:
            del self._idle[key]

    @staticmethod
    def _close(obj):
        try:
            closer = getattr(obj, "disconnect", None) or obj.close
            closer()
        except Exception:
            METRICS.add("pool_close_errors")

    def _checkout(self, key, factory):
        if self._pid != os.getpid():
            # Do not ever reuse sockets created in a parent process.
            raise RuntimeError("Data API pools were initialized before fork. Disable server preload.")
        deadline = time.monotonic() + self.wait_seconds
        while True:
            stale, chosen, reserved = [], None, False
            with self._cv:
                if self._closed:
                    raise CapacityError("Pool is closing")
                now = time.monotonic()
                for k in list(self._idle):
                    keep = []
                    for entry in self._idle[k]:
                        if now - entry.idle_since > self.idle_seconds or now - entry.created_at > self.lifetime_seconds:
                            stale.append(entry.connection)
                            self._open[k] -= 1
                        else:
                            keep.append(entry)
                    if keep:
                        self._idle[k] = keep
                    else:
                        del self._idle[k]
                    if not self._open[k]:
                        del self._open[k]
                if self._idle.get(key):
                    chosen = self._idle[key].pop()
                    if not self._idle[key]:
                        del self._idle[key]
                else:
                    # Reclaim an idle connection to a different database when at
                    # the global cap. Its slot is reserved for this factory.
                    if sum(self._open.values()) >= self.max_total and self._open[key] < self.max_per_key:
                        donor = next((k for k, entries in self._idle.items() if k != key and entries), None)
                        if donor is not None:
                            entry = self._idle[donor].pop()
                            stale.append(entry.connection)
                            self._drop_locked(donor)
                    if sum(self._open.values()) < self.max_total and self._open[key] < self.max_per_key:
                        self._open[key] += 1
                        reserved = True
                if chosen is not None or reserved:
                    self._busy += 1
                    self.high_watermark = max(self.high_watermark, sum(self._open.values()))
                remaining = deadline - now
            for obj in stale:
                self._close(obj)
            if chosen is not None:
                return chosen
            if reserved:
                try:
                    obj = factory()
                    return _Idle(obj, time.monotonic(), time.monotonic())
                except BaseException:
                    with self._cv:
                        self._busy -= 1
                        self._drop_locked(key)
                        self._cv.notify_all()
                    raise
            if remaining <= 0:
                METRICS.add("pool_checkout_rejected")
                raise CapacityError("Source capacity is busy; retry with backoff")
            with self._cv:
                self._cv.wait(min(remaining, 0.02))

    def _return(self, key, entry, healthy):
        close = False
        with self._cv:
            self._busy -= 1
            now = time.monotonic()
            if (not healthy or self._closed or now - entry.created_at > self.lifetime_seconds
                    or len(self._idle.get(key, [])) >= self.max_idle_per_key):
                self._drop_locked(key)
                close = True
            else:
                entry.idle_since = now
                self._idle.setdefault(key, []).append(entry)
            self._cv.notify_all()
        if close:
            self._close(entry.connection)

    @contextmanager
    def connection(self, key, factory: Callable):
        entry = self._checkout(key, factory)
        healthy = False
        try:
            yield entry.connection
            healthy = not bool(getattr(getattr(entry.connection, "conn", entry.connection), "closed", False))
        except HTTPException:
            # Request validation is not a broken database socket. Vector-query
            # transaction cleanup happens in the connector before this point.
            healthy = not bool(getattr(getattr(entry.connection, "conn", entry.connection), "closed", False))
            raise
        finally:
            self._return(key, entry, healthy)

    def snapshot(self):
        with self._cv:
            return {"open": sum(self._open.values()), "busy": self._busy,
                    "idle": sum(map(len, self._idle.values())),
                    "max_total": self.max_total, "max_per_key": self.max_per_key,
                    "high_watermark": self.high_watermark, "scope": "process"}

    def close(self):
        with self._cv:
            self._closed = True
            idle = [(key, e) for key, entries in self._idle.items() for e in entries]
            self._idle.clear()
            for key, _ in idle:
                self._open[key] -= 1
                if not self._open[key]:
                    del self._open[key]
            self._cv.notify_all()
        for _, entry in idle:
            self._close(entry.connection)


class AdmissionGate:
    def __init__(self, total: int, per_caller: int):
        self.total, self.per_caller = total, per_caller
        self._lock = threading.Lock()
        self._active = Counter()
        self._count = 0

    def enter(self, caller):
        with self._lock:
            if self._count >= self.total:
                raise HTTPException(503, "Data API capacity is busy. Retry with backoff.", headers={"Retry-After": "1"})
            if self._active[caller] >= self.per_caller:
                raise HTTPException(429, "Too many concurrent requests for this API key.", headers={"Retry-After": "1"})
            self._count += 1
            self._active[caller] += 1

    def leave(self, caller):
        with self._lock:
            self._count -= 1
            self._active[caller] -= 1
            if not self._active[caller]:
                del self._active[caller]

    def snapshot(self):
        with self._lock:
            return {"active": self._count, "limit": self.total, "per_key_limit": self.per_caller,
                    "scope": "process; use shared metadata rate limits or an API gateway across workers"}


HTTP_GATE = AdmissionGate(env_int("DATA_API_MAX_INFLIGHT", 16, maximum=1024),
                          env_int("DATA_API_MAX_INFLIGHT_PER_KEY", 4, maximum=1024))
_EXACT_TOTAL = env_int("DATA_API_EXACT_MAX_INFLIGHT", 2, maximum=128)
# Keyed per API key by the callers. Defaults to the total so current behaviour
# is unchanged; set it lower (e.g. 1) so one key cannot hold every exact slot.
EXACT_GATE = AdmissionGate(_EXACT_TOTAL,
                           env_int("DATA_API_EXACT_MAX_INFLIGHT_PER_KEY", _EXACT_TOTAL, maximum=128))
BODY_TIMEOUT_SECONDS = env_int("DATA_API_VECTOR_BODY_TIMEOUT_SECONDS", 10, maximum=60)
MAX_BODY_BYTES = env_int("DATA_API_VECTOR_MAX_BODY_BYTES", 524288, maximum=8_388_608)
MAX_RESPONSE_BYTES = env_int("DATA_API_VECTOR_MAX_RESPONSE_BYTES", 8_388_608, maximum=134_217_728)


class DataAPIRoute(APIRoute):
    """Enforce admission before body parsing or the shared synchronous threadpool."""
    def get_route_handler(self):
        original = super().get_route_handler()
        operation = self.path.rsplit("/", 1)[-1]
        consumer = operation in {"rows", "columns", "count", "tables", "vector-search", "retrieve"} and "{api_id}" in self.path
        json_post = operation in {"vector-search", "retrieve"}

        async def handler(request: Request):
            started = time.monotonic()
            request_id = uuid.uuid4().hex
            admitted = False
            status = 500
            caller = ""
            try:
                if consumer:
                    credential = request.headers.get("x-api-key") or request.query_params.get("api_key", "")
                    caller = hashlib.sha256(credential.encode()).hexdigest() if credential else "unauthenticated"
                    HTTP_GATE.enter(caller)
                    admitted = True
                if json_post and request.method == "POST":
                    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type != "application/json":
                        raise HTTPException(415, "Send application/json.")
                    length = request.headers.get("content-length")
                    if length is not None:
                        try:
                            declared = int(length)
                        except ValueError:
                            raise HTTPException(400, "Invalid Content-Length.") from None
                        if declared < 0:
                            raise HTTPException(400, "Invalid Content-Length.")
                        if declared > MAX_BODY_BYTES:
                            raise HTTPException(413, "Request exceeds the configured byte limit.")
                    async def read_bounded_body():
                        chunks, size = [], 0
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > MAX_BODY_BYTES:
                                raise HTTPException(413, "Request exceeds the configured byte limit.")
                            chunks.append(chunk)
                        return b"".join(chunks)
                    try:
                        request._body = await asyncio.wait_for(read_bounded_body(), BODY_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        raise HTTPException(408, "Request body was not received within the time limit.") from None
                response = await original(request)
                status = response.status_code
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Request-ID"] = request_id
                return response
            except RequestValidationError as exc:
                status = 422
                # FastAPI's normal validation response includes the rejected
                # input. Do not echo API keys, vectors or unbounded input here.
                errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
                return JSONResponse({"detail": errors}, status_code=422,
                                    headers={"Cache-Control": "no-store", "X-Request-ID": request_id})
            except HTTPException as exc:
                status = exc.status_code
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store", "X-Request-ID": request_id}
                raise
            finally:
                if admitted:
                    HTTP_GATE.leave(caller)
                METRICS.add(f"http_{status}")
                METRICS.add("http_elapsed_ms_total", int((time.monotonic() - started) * 1000))
        return handler


class TTLCache:
    """Bounded LRU storage; TTL is checked at read. Authorization is never cached."""
    def __init__(self, capacity=2048):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._items = OrderedDict()

    def get(self, key, ttl):
        if ttl <= 0:
            return None
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            value, when = item
            if time.monotonic() - when > ttl:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key, value):
        with self._lock:
            self._items[key] = (value, time.monotonic())
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def evict(self, table_ids=None):
        with self._lock:
            if table_ids is None:
                self._items.clear()
            else:
                targets = set(table_ids)
                for key in list(self._items):
                    # Keys are (table_id, field); fingerprint is part of field.
                    if key[0] in targets:
                        del self._items[key]