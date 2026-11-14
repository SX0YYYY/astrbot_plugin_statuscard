"""Platform compatibility layer for the status card.

The plugin originally targeted ``aiocqhttp`` (OneBot). A QQ official bot
(``qq_official`` / ``qq_official_webhook``) exposes a different surface:

* senders are opaque openids, not QQ numbers;
* there is no friend list, group list, login info or avatar URL;
* group member counts come from a per-group info API;
* proactive group sends depend on an in-memory scene cache, and AstrBot's
  ``send_by_session`` silently skips a send when that cache is empty.

Everything platform-specific lives here so collectors / delivery / alerts do
not grow their own ``if qq_official`` branches.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from astrbot.api import logger

SUPPORTED_PLATFORMS = ("aiocqhttp", "qq_official", "qq_official_webhook")
OFFICIAL_PLATFORMS = ("qq_official", "qq_official_webhook")
OFFICIAL_LABEL = "QQ 官方机器人"


def normalize_platform_name(name: object) -> str:
    return str(name or "").strip().lower()


def is_supported_platform(name: object) -> bool:
    """An empty name still renders a degraded card (tests, internal callers)."""

    text = normalize_platform_name(name)
    return not text or text in SUPPORTED_PLATFORMS


def is_official_platform(name: object) -> bool:
    return normalize_platform_name(name) in OFFICIAL_PLATFORMS


def platform_label(name: object) -> str:
    text = normalize_platform_name(name)
    if text == "aiocqhttp":
        return "OneBot"
    if is_official_platform(text):
        return OFFICIAL_LABEL
    return text or "--"


def _iter_instances(context: Any) -> list[Any]:
    if context is None:
        return []
    try:
        manager = getattr(context, "platform_manager", None)
        if manager is None:
            return []
        if hasattr(manager, "get_insts"):
            insts = manager.get_insts()
        else:
            insts = getattr(manager, "platform_insts", [])
        return list(insts or [])
    except Exception as exc:
        logger.debug("枚举平台实例失败: %s", exc)
        return []


def _meta(inst: Any) -> Any:
    try:
        return (
            inst.meta()
            if callable(getattr(inst, "meta", None))
            else getattr(inst, "meta", None)
        )
    except Exception:
        return None


def find_adapter(
    context: Any,
    platform_name: str | None = None,
    platform_id: str | None = None,
) -> Any:
    """Return the live platform adapter matching ``meta().name`` / ``meta().id``."""

    want_name = normalize_platform_name(platform_name)
    for inst in _iter_instances(context):
        meta = _meta(inst)
        name = normalize_platform_name(getattr(meta, "name", ""))
        if not name:
            continue
        if want_name and name != want_name:
            continue
        inst_id = str(getattr(meta, "id", "") or "")
        if platform_id and inst_id and inst_id != str(platform_id):
            continue
        return inst
    return None


def find_official_adapter(context: Any, platform_id: str | None = None) -> Any:
    for name in OFFICIAL_PLATFORMS:
        inst = find_adapter(context, name, platform_id)
        if inst is not None:
            return inst
    return None


def adapter_platform_name(inst: Any) -> str:
    meta = _meta(inst)
    return normalize_platform_name(getattr(meta, "name", ""))


def adapter_platform_id(inst: Any) -> str:
    meta = _meta(inst)
    return str(getattr(meta, "id", "") or "")


def adapter_client(inst: Any) -> Any:
    if inst is None:
        return None
    getter = getattr(inst, "get_client", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return getattr(inst, "client", None)


def adapter_running(inst: Any) -> bool | None:
    """True/False when the adapter exposes a status; None when unknown."""

    status = getattr(inst, "status", None)
    if status is None:
        return None
    value = getattr(status, "value", status)
    if value is None:
        return None
    return str(value).strip().lower() == "running"


def parse_umo(umo: object) -> tuple[str, str, str] | None:
    parts = str(umo or "").split(":")
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def group_session_id(session_id: object) -> str:
    """group_openid from a UMO session id.

    AstrBot's ``unique_session`` prefixes the sender openid, and the official
    adapter restores the group id with ``rsplit("_", 1)[-1]``. Mirror it here
    so the scene cache is written under the key the sender reads.
    """

    return str(session_id or "").rsplit("_", 1)[-1]


def ensure_group_scene(
    adapter: Any, umos: list[str] | tuple[str, ...]
) -> tuple[bool, list[str]]:
    """Prime the official adapter's scene cache for group targets.

    ``send_by_session`` reports success (returns None) but drops a group send
    when ``_session_scene`` has no entry, which happens after a restart until
    the group posts again. Returns ``(ok, failed_targets)``; non-official
    adapters are a no-op and always succeed.
    """

    if adapter is None:
        return False, [str(item) for item in umos]
    if not is_official_platform(adapter_platform_name(adapter)):
        return True, []
    remember = getattr(adapter, "remember_session_scene", None)
    if not callable(remember):
        # An older AstrBot without the helper: nothing to prime, and failing
        # every send would be worse than letting the framework try.
        return True, []
    failed: list[str] = []
    scenes = getattr(adapter, "_session_scene", None)
    for umo in umos:
        parsed = parse_umo(umo)
        if parsed is None:
            continue
        _, message_type, session_id = parsed
        if message_type != "GroupMessage":
            continue
        target_id = group_session_id(session_id)
        if isinstance(scenes, dict) and scenes.get(target_id) == "channel":
            # QQ channels also arrive as GroupMessage but post through the
            # channel API. Overwriting their cached scene with "group" would
            # route the next send to post_group_message (v2.21.0 audit).
            continue
        try:
            remember(target_id, "group")
        except Exception as exc:
            logger.debug("注入群 scene 失败: %s", exc)
            failed.append(str(umo))
    return not failed, failed


def astrbot_data_db() -> Path | None:
    """Read-only path to AstrBot's data_v4.db, or None when unavailable."""

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        path = Path(get_astrbot_data_path()) / "data_v4.db"
        return path if path.is_file() else None
    except Exception as exc:
        logger.debug("定位 AstrBot 数据库失败: %s", exc)
        return None


def known_group_session_ids(
    db_path: Path | str | None, platform_id: str | None = None
) -> list[str]:
    """Group session ids AstrBot has seen for this platform, newest first.

    The official bot has no group-list API, so ``umo_senders`` is the only
    local source. Groups the bot has never received a message from are not
    listed -- callers must label the number as "known groups".
    """

    if db_path is None:
        return []
    path = Path(db_path)
    if not path.is_file():
        return []
    # ``_`` is a LIKE wildcard and platform ids commonly contain it
    # ("qq_official"), which would match other instances ("qqXofficial").
    escaped = (
        str(platform_id or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    pattern = f"{escaped}:GroupMessage:%" if platform_id else "%:GroupMessage:%"
    try:
        # Percent-encode '?'/'#' exactly like provider_quality.connect_readonly
        # does: they are legal filename characters but delimit the query part
        # of a SQLite URI (v2.21.3).
        ro_path = path.as_posix().replace("?", "%3F").replace("#", "%23")
        connection = sqlite3.connect(f"file:{ro_path}?mode=ro", uri=True, timeout=2)
        try:
            # Deduplicate in Python, not with ``GROUP BY umo``: AstrBot's
            # ``unique_session`` makes the UMO ``...:sender_group``, so
            # grouping by the full UMO kept one row per sender and a single
            # busy group could fill LIMIT 200, hiding every other group
            # (v2.21.1 audit). 4000 recent rows are a few hundred KB at most.
            rows = connection.execute(
                "SELECT umo FROM umo_senders WHERE umo LIKE ? ESCAPE '\\' "
                "ORDER BY rowid DESC LIMIT 4000",
                (pattern,),
            ).fetchall()
        finally:
            connection.close()
    except Exception as exc:
        logger.debug("读取 umo_senders 失败: %s", exc)
        return []
    seen: list[str] = []
    for (umo,) in rows:
        parsed = parse_umo(umo)
        if parsed is None:
            continue
        session_id = group_session_id(parsed[2])
        if session_id and session_id not in seen:
            seen.append(session_id)
            if len(seen) >= 200:
                break
    return seen


async def group_member_total(adapter: Any, group_ids: list[str]) -> int | None:
    """Sum ``group_member_num`` over known groups via the official API.

    Returns None when the platform/API is unavailable or no group responded,
    so the caller can render ``--`` instead of a misleading 0.
    """

    if adapter is None or not group_ids:
        return None
    if not is_official_platform(adapter_platform_name(adapter)):
        return None
    client = adapter_client(adapter)
    if client is None:
        return None
    try:
        from astrbot.core.platform.astrbot_message import Group
        from astrbot.core.platform.sources.qqofficial.qqofficial_group_info import (
            get_qq_group,
        )
    except Exception as exc:
        logger.debug("官方群信息接口不可用: %s", exc)
        return None

    async def fetch_one(group_id: str) -> int | None:
        group = await get_qq_group(client, Group(group_id=group_id))
        count = getattr(group, "member_count", None)
        return int(count) if count is not None else None

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(fetch_one(group_id) for group_id in group_ids),
                return_exceptions=True,
            ),
            timeout=8.0,
        )
    except TimeoutError:
        logger.debug("群员汇总超时（%d 个已知群）", len(group_ids))
        return None
    total = 0
    for result in results:
        if isinstance(result, BaseException) or result is None:
            # A partial sum would be frozen into today's report snapshot as
            # complete and undercount forever. Unknown is safer than low.
            logger.debug("群员汇总不完整，按未知处理")
            return None
        total += result
    return total


def send_stats_platform_ids() -> tuple[str, ...]:
    """platform_stats stores the adapter name in platform_id (type is unknown)."""

    return ("aiocqhttp",) + OFFICIAL_PLATFORMS
