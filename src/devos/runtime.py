"""Bounded HTTP transport, persistent provider cooldowns, and snapshot replay."""

from __future__ import annotations

import email.utils
import hashlib
import os
import random
import socket
import time
import urllib.error
import urllib.request
from datetime import timezone
from typing import Any
from urllib.parse import urlsplit

from devos.core import DevosError, uid
from devos.ports import Document, Response, Storage, Transport

MAX_BYTES = 5 * 1024 * 1024


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...] = ("api.github.com",)):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc not in self.allowed_hosts:
            raise DevosError("access_denied", "Provider redirected outside its permitted HTTPS hosts.")
        count = getattr(req, "devos_redirects", 0)
        if count >= 3:
            raise DevosError("transient_network", "Provider redirect limit exceeded.")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            setattr(redirected, "devos_redirects", count + 1)
        return redirected


class HTTPTransport:
    def __init__(self, *, timeout: float = 15, max_bytes: int = MAX_BYTES,
                 allowed_hosts: tuple[str, ...] = ("api.github.com",)):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.allowed_hosts = allowed_hosts
        self.opener = urllib.request.build_opener(SafeRedirect(allowed_hosts))

    def get(self, url: str, headers: dict[str, str]) -> Response:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in self.allowed_hosts:
            raise DevosError("access_denied", "This adapter permits only its declared HTTPS provider hosts.")
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise DevosError("transient_network", "Provider could not be reached.", retry_at=time.time() + 5) from None
        with response:
            if response.status >= 400 or response.status == 304:
                body = b""  # Error bodies are unnecessary and may contain sensitive request details.
            else:
                try:
                    body = response.read(self.max_bytes + 1)
                except (OSError, TimeoutError):
                    raise DevosError("transient_network", "Provider response was interrupted.", retry_at=time.time() + 5) from None
                if len(body) > self.max_bytes:
                    raise DevosError("invalid_payload", "Provider response exceeds the configured size limit.")
            safe_headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in {
                "content-type", "etag", "last-modified", "retry-after", "x-ratelimit-reset",
                "x-ratelimit-remaining", "x-ratelimit-resource",
            }}
            return Response(response.status, safe_headers, body)


class SnapshotFetcher:
    def __init__(self, store: Storage, transport: Transport, *, adapter: str, offline: bool = False,
                 replay: bool = False, snapshots: dict[str, str] | None = None,
                 token: str | None = None, clock: Any = time.time, min_interval: float = 0):
        self.store, self.transport, self.adapter = store, transport, adapter
        self.offline, self.replay = offline, replay
        self.snapshots = snapshots or {}
        env_name = {"github": "GITHUB_TOKEN", "huggingface": "HF_TOKEN"}.get(adapter, "")
        self.token = os.environ.get(env_name, "") if token is None else token
        self.min_interval = min_interval
        self.clock = clock
        self.cache_scope = "public:" + (hashlib.sha256(self.token.encode()).hexdigest() if self.token else "anonymous")
        self.quota_key = f"{adapter}:{self.cache_scope}:core"

    def _cached(self, cached: Any, resource: str, url: str) -> Document:
        if not cached:
            raise DevosError("replay_unavailable", "No retained snapshot exists in this credential context.")
        if self.replay:
            return Document(cached["id"], cached["body"], "replay", cached["status"])
        sid = self.store.put_snapshot(self.adapter, resource, url, cached["body"], cached["status"],
                                      cached, self.cache_scope, observed_at=cached["observed_at"])
        return Document(sid, cached["body"], "cached", cached["status"])

    def _deadline(self, headers: dict[str, str]) -> float:
        now = self.clock()
        deadlines = [now + 60]
        if headers.get("retry-after"):
            try:
                deadlines.append(now + max(0, float(headers["retry-after"])))
            except ValueError:
                try:
                    stamp = email.utils.parsedate_to_datetime(headers["retry-after"])
                    deadlines.append(stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp())
                except (TypeError, ValueError, OverflowError):
                    pass
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                deadlines.append(float(headers.get("x-ratelimit-reset", "0")) + 1)
            except ValueError:
                pass
        return max(deadlines)

    def fetch(self, resource_key: str, url: str) -> Document:
        cached = self.store.latest_snapshot(self.adapter, resource_key, self.cache_scope)
        if resource_key in self.snapshots:
            cached = self.store.snapshot(self.snapshots[resource_key])
            if cached["adapter_key"] != self.adapter or cached["resource_key"] != resource_key:
                raise DevosError("invalid_input", "Snapshot does not match the requested resource.")
            if cached["cache_scope"] != self.cache_scope:
                raise DevosError("access_denied", "Snapshot belongs to a different credential context.")
            return Document(cached["id"], cached["body"], "replay", cached["status"])
        if self.offline or self.replay:
            return self._cached(cached, resource_key, url)

        now = self.clock()
        reservation = uid()
        with self.store.transaction():
            self.store.execute("INSERT OR IGNORE INTO provider_state(quota_key) VALUES (?)", (self.quota_key,))
            state = self.store.one("SELECT * FROM provider_state WHERE quota_key=?", (self.quota_key,))
            assert state
            deadline = max(state["next_request_at"], state["circuit_until"])
            if deadline <= now:
                # Reserve the provider slot across processes. No lock is held during HTTP.
                self.store.execute("UPDATE provider_state SET next_request_at=?,lease_token=? WHERE quota_key=?",
                                   (now + 90, reservation, self.quota_key))
        if deadline > now:
            if self.min_interval and deadline - now <= self.min_interval + 0.05:
                time.sleep(deadline - now)
                return self.fetch(resource_key, url)
            if cached:
                return self._cached(cached, resource_key, url)
            raise DevosError("rate_limited", "Provider cooldown is active; resume the job after its deadline.", retry_at=deadline)

        headers = {"Accept": "application/atom+xml" if self.adapter == "arxiv" else "application/json", "User-Agent": "devos-vault/0.3.0"}
        if self.adapter == "github":
            headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached and cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        try:
            response = self.transport.get(url, headers)
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            if self.token:
                response_headers = {key: value.replace(self.token, "[REDACTED]") for key, value in response_headers.items()}
            if response.status == 429 or (response.status == 403 and (
                response_headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response_headers
            )):
                deadline = self._deadline(response_headers)
                with self.store.transaction():
                    self.store.execute("UPDATE provider_state SET next_request_at=? WHERE quota_key=? AND lease_token=?",
                                       (deadline, self.quota_key, reservation))
                raise DevosError("rate_limited", "Provider rate limit reached; resume after the recorded deadline.", retry_at=deadline)
            if response.status == 401:
                raise DevosError("auth_required", "Provider rejected the configured credential.")
            if response.status == 403:
                raise DevosError("access_denied", "Provider denied access to this resource.")
            if response.status == 404:
                raise DevosError("not_found", "Provider resource was not found or is inaccessible.")
            if response.status >= 500:
                raise DevosError("transient_network", "Provider is temporarily unavailable.", retry_at=now + 5)
            if response.status not in {200, 304}:
                raise DevosError("invalid_payload", "Unexpected Provider response status.")
            if response.status == 304:
                if not cached:
                    raise DevosError("invalid_payload", "Provider revalidated a resource without a retained body.")
                body = cached["body"]
                prior_headers = {key: cached[column] for key, column in (
                    ("etag", "etag"), ("last-modified", "last_modified"),
                ) if cached.get(column) is not None}
                response_headers = {**prior_headers, **response_headers}
            else:
                body = response.body
            if self.token:
                body = body.replace(self.token.encode(), b"[REDACTED]")
            sid = self.store.put_snapshot(self.adapter, resource_key, url, body, response.status,
                                          response_headers, self.cache_scope)
            with self.store.transaction():
                self.store.execute("UPDATE provider_state SET failures=0,circuit_until=0,next_request_at=? WHERE quota_key=? AND lease_token=?",
                                   (self.clock() + self.min_interval if self.min_interval else 0, self.quota_key, reservation))
                if response_headers.get("x-ratelimit-remaining") == "0":
                    self.store.execute("UPDATE provider_state SET next_request_at=? WHERE quota_key=? AND lease_token=?",
                                       (self._deadline(response_headers), self.quota_key, reservation))
            return Document(sid, body, "revalidated" if response.status == 304 else "fresh", response.status)
        except DevosError as exc:
            if exc.code == "transient_network":
                with self.store.transaction():
                    failures = self.store.one("SELECT failures FROM provider_state WHERE quota_key=?", (self.quota_key,))
                    count = (failures or {}).get("failures", 0) + 1
                    self.store.execute("UPDATE provider_state SET failures=?,circuit_until=?,next_request_at=0 WHERE quota_key=? AND lease_token=?",
                                       (count, now + 60 if count >= 3 else 0, self.quota_key, reservation))
                exc.retry_at = now + min(60, 2 ** count) + random.uniform(0, 1)
            elif exc.code != "rate_limited":
                with self.store.transaction():
                    self.store.execute("UPDATE provider_state SET next_request_at=0 WHERE quota_key=? AND lease_token=?", (self.quota_key, reservation))
            if cached and exc.code in {"transient_network", "rate_limited"}:
                return self._cached(cached, resource_key, url)
            raise
