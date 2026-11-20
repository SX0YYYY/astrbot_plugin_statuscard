"""Dead Man's Switch heartbeat listener.

External programs POST ``{"name": "..."}`` to a local HTTP endpoint every
period; the service monitor's ``heartbeat`` probe turns a missed heartbeat
into the same alert/recovery state machine every other monitor item uses.
The listener only writes ``state["monitors"]["heartbeat"][name]`` -- it
never sends messages, never creates state keys for unknown names, and never
logs or stores the token.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from typing import Any

from astrbot.api import logger

from .collectors import cfg_bool, cfg_get, cfg_int

MAX_BODY_BYTES = 64 * 1024


class HeartbeatServer:
    """Local HTTP endpoint that records ``last_heartbeat`` per known name."""

    def __init__(self, state: Any, config: Any, monitor: Any = None):
        self.state = state
        self.config = config
        self._monitor = monitor
        self._runner: Any = None
        self._site: Any = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            # start() is not idempotent: a second call would create a second
            # listener that loses the first runner's handle (the Komari
            # webhook start() had the same trap, v2.21.3).
            logger.debug("心跳监听已在运行，忽略重复启动")
            return
        if not cfg_bool(self.config, "monitors.heartbeat_server.enabled", False):
            return
        try:
            self._task = asyncio.create_task(self._start())
            self._task.add_done_callback(self._on_task_done)
        except Exception:
            logger.exception("心跳监听任务创建失败")
            self._task = None

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("心跳监听任务异常退出: %s", exc)

    async def _start(self) -> None:
        if not cfg_bool(self.config, "monitors.heartbeat_server.enabled", False):
            return
        try:
            from aiohttp import web
        except Exception as exc:
            logger.error("无法导入 aiohttp，心跳监听未启动: %s", exc)
            return

        # Same normalisation as the Komari webhook: a whitespace-only host is
        # a loopback bind, not a wildcard one (v2.20.32).
        host = (
            str(
                cfg_get(self.config, "monitors.heartbeat_server.host", "127.0.0.1")
                or ""
            ).strip()
            or "127.0.0.1"
        )
        port = max(
            1, min(65535, cfg_int(self.config, "monitors.heartbeat_server.port", 9969))
        )
        path = str(
            cfg_get(self.config, "monitors.heartbeat_server.path", "/heartbeat")
            or "/heartbeat"
        )
        if not path.startswith("/"):
            path = "/" + path
        require_token = cfg_bool(
            self.config, "monitors.heartbeat_server.require_token", True
        )
        if require_token and not any(self._reporters().values()):
            logger.error(
                "心跳接收服务已开启「强制要求 Token」，但没有一个心跳监测项配置了 "
                "Token，所有上报都会被拒绝，已拒绝启动；请先给心跳监测项填写 Token"
            )
            return
        if not self._bind_allowed(host, require_token):
            # Same rule as the Komari webhook, adjusted for per-item tokens:
            # a wildcard bind without an enforced token turns the endpoint
            # into an open reporter that can latch fake alerts into the
            # operator's chat. With per-item tokens the enforced-token half
            # is "require_token is on" (the items' tokens are validated in
            # the handler); the server itself holds no token, so the
            # alerts._webhook_bind_allowed helper does not apply verbatim.
            logger.error(
                "心跳监听所有网卡（%s）但没有启用 Token 鉴权，已拒绝启动；"
                "请改用 127.0.0.1，或勾选「强制要求 Token」并给心跳监测项填写 Token",
                host,
            )
            return

        app = web.Application()

        async def handle(request: web.Request) -> web.Response:
            return await self._handle(request, require_token)

        app.router.add_post(path, handle)
        runner = web.AppRunner(app)
        # Assign before setup/start so stop() can always reach the runner,
        # exactly like the Komari webhook (v2.21.3): a cancel landing between
        # setup() and start() would otherwise leak the listener.
        self._runner = runner
        try:
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
        except BaseException as exc:
            self._runner = None
            try:
                await runner.cleanup()
            except Exception:
                pass
            if isinstance(exc, Exception):
                logger.error("心跳监听启动失败 %s:%s: %s", host, port, exc)
                return
            raise
        self._site = site
        logger.info("心跳接收服务已监听 http://%s:%s%s", host, port, path)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        runner = self._runner
        self._runner = None
        self._site = None
        if runner is not None:
            try:
                await runner.cleanup()
                logger.info("心跳接收服务已停止")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # request handling
    # ------------------------------------------------------------------

    @staticmethod
    def _bind_allowed(host: str, require_token: bool) -> bool:
        """Whether the listener may bind ``host`` with this auth setting.

        Mirror of ``alerts._webhook_bind_allowed`` for per-item tokens: a
        wildcard bind is only acceptable when token checking is enforced
        (each reporter must present its own token in the handler).
        """

        normalized = host.strip().strip("[]").lower()
        if normalized not in {"0.0.0.0", "::", "0:0:0:0:0:0:0:0", ""}:
            return True
        return bool(require_token)

    def _reporters(self) -> dict[str, str]:
        """Enabled, fully configured heartbeat items: name -> token."""

        monitor = self._monitor
        items = getattr(monitor, "items", None)
        if not isinstance(items, list):
            return {}
        reporters: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("__template_key")) != "heartbeat":
                continue
            if item.get("config_error"):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                reporters[name] = str(item.get("token") or "").strip()
        return reporters

    async def _handle(self, request: Any, require_token: bool) -> Any:
        from aiohttp import web

        length = request.content_length
        if length is not None and length > MAX_BODY_BYTES:
            return web.json_response(
                {"status": "error", "message": "payload too large"}, status=413
            )
        raw = await request.content.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            return web.json_response(
                {"status": "error", "message": "payload too large"}, status=413
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.json_response(
                {"status": "error", "message": "invalid json"}, status=400
            )
        if not isinstance(data, dict):
            return web.json_response(
                {"status": "error", "message": "expected an object"}, status=400
            )
        name = str(data.get("name") or "").strip()
        if not name:
            return web.json_response(
                {"status": "error", "message": "name missing"}, status=400
            )
        reporters = self._reporters()
        item_token = reporters.get(name)
        if item_token is None:
            # An unknown name must not create a state key: the probe only
            # answers to configured items, and a typo in the reporter would
            # otherwise grow the state file forever.
            return web.json_response(
                {"status": "error", "message": "unknown name"}, status=404
            )
        # Per-item token check (the switch decides whether it is enforced).
        # The token itself never reaches a log line.
        if require_token:
            auth = request.headers.get("Authorization", "")
            scheme, _, credentials = auth.partition(" ")
            if (
                scheme.lower() != "bearer"
                or not item_token
                or not hmac.compare_digest(
                    credentials.strip().encode("utf-8"), item_token.encode("utf-8")
                )
            ):
                return web.json_response(
                    {"status": "error", "message": "unauthorized"}, status=403
                )

        now = time.time()
        with self.state.section("monitors") as monitors:
            heartbeat = monitors.get("heartbeat")
            if not isinstance(heartbeat, dict):
                # A hand-edited state.json may hold a list/scalar here; the
                # section helper pattern replaces it with a dict (same guard
                # as monitors.items in the probe loop).
                heartbeat = {}
                monitors["heartbeat"] = heartbeat
            entry = heartbeat.get(name)
            if not isinstance(entry, dict):
                entry = {}
                heartbeat[name] = entry
            # Only the fields with a consumer: the probe reads last_heartbeat
            # and heartbeat_first_seen. A counter had a raw int() here once,
            # which turned a hand-edited dirty value into a 500 on EVERY
            # report (and skipped that save) -- a field nobody reads is not
            # worth that risk (reverse audit P3-3).
            entry["last_heartbeat"] = now
        # Persist promptly: a crash between the in-memory update and the next
        # periodic save would otherwise report a healthy reporter as dead
        # after a restart. state.save() serialises concurrent callers.
        await asyncio.to_thread(self.state.save)
        return web.json_response({"status": "ok"})

    # ------------------------------------------------------------------
    # introspection (diagnostics / audit)
    # ------------------------------------------------------------------

    def endpoint(self) -> str:
        """The configured ``host:port path``, for the dashboard/diag output."""

        host = (
            str(
                cfg_get(self.config, "monitors.heartbeat_server.host", "127.0.0.1")
                or ""
            ).strip()
            or "127.0.0.1"
        )
        port = max(
            1, min(65535, cfg_int(self.config, "monitors.heartbeat_server.port", 9969))
        )
        path = str(
            cfg_get(self.config, "monitors.heartbeat_server.path", "/heartbeat")
            or "/heartbeat"
        )
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{host}:{port}{path}"
