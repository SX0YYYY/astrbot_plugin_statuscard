from __future__ import annotations

import asyncio
import logging
import math
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

try:
    import pwd
except ImportError:  # Windows development environment
    pwd = None  # type: ignore[assignment]
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil

from .utils import CpuFreq, readable_python_version
from .version_resolver import resolve_astrbot_version

logger = logging.getLogger("astrbot_plugin_statuscard")


def _dt_now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


BOOT_TIME = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).astimezone()
PROCESS_START_TIME = datetime.fromtimestamp(
    psutil.Process(os.getpid()).create_time(), tz=timezone.utc
).astimezone()


def format_bytes(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "--"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "--"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while abs(value) >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{value:.0f} {units[idx]}"
    return f"{value:.{digits}f} {units[idx]}"


def format_speed(value: float | int | None) -> str:
    return f"{format_bytes(value, 1)}/s"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "--"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "--"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    body = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}天{body}" if days else body


def format_duration_td(dt: timedelta) -> str:
    return format_duration(dt.total_seconds())


def _format_td(dt: timedelta) -> str:
    return format_duration_td(dt)


def cfg_get(config: Any, path: str, default: Any = None) -> Any:
    cur = config
    for part in path.split("."):
        if cur is None:
            return default
        if hasattr(cur, "get"):
            cur = cur.get(part)
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return default
    return default if cur is None else cur


def cfg_bool(config: Any, path: str, default: bool) -> bool:
    value = cfg_get(config, path, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def cfg_int(config: Any, path: str, default: int) -> int:
    try:
        value = cfg_get(config, path, default)
        if isinstance(value, bool):
            return default
        return int(value)
    except Exception:
        return default


def cfg_float(config: Any, path: str, default: float) -> float:
    try:
        value = cfg_get(config, path, default)
        if isinstance(value, bool):
            return default
        return float(value)
    except Exception:
        return default


def get_cpu_brand() -> str:
    brand = ""
    try:
        if platform.system() == "Linux":
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        brand = line.split(":", 1)[1].strip()
                        break
        else:
            brand = platform.processor() or platform.machine()
    except Exception:
        brand = ""
    brand = brand.strip()
    if not brand:
        return "Unknown CPU"
    if brand.lower().endswith(("cpu", "processor")):
        brand = brand.rsplit(" ", 1)[0]
    return brand


def cpu_count() -> int | None:
    return psutil.cpu_count(logical=False)


def cpu_count_logical() -> int | None:
    return psutil.cpu_count()


def cpu_percent() -> float:
    return psutil.cpu_percent(interval=None)


def cpu_freq() -> CpuFreq:
    freq = psutil.cpu_freq()
    return CpuFreq(
        current=getattr(freq, "current", None),
        min=getattr(freq, "min", None),
        max=getattr(freq, "max", None),
    )


@dataclass
class MemStat:
    total: int
    used: int
    percent: float


def memory_stat() -> MemStat:
    m = psutil.virtual_memory()
    return MemStat(total=m.total, used=m.used, percent=m.percent)


def swap_stat() -> MemStat:
    s = psutil.swap_memory()
    return MemStat(total=s.total, used=s.used, percent=s.percent)


def _default_disk_paths() -> list[str]:
    if platform.system() == "Windows":
        return [os.path.abspath(os.sep)]
    return ["/"]


@dataclass
class DiskUsage:
    name: str
    used: int | None
    total: int | None
    percent: float | None
    exception: str | None = None


def disk_usage(paths: list[str] | None = None) -> list[DiskUsage]:
    targets = paths or _default_disk_paths()
    ret: list[DiskUsage] = []
    for name in targets:
        try:
            u = psutil.disk_usage(name)
            ret.append(
                DiskUsage(name=name, used=u.used, total=u.total, percent=u.percent)
            )
        except Exception as exc:
            ret.append(
                DiskUsage(
                    name=name, used=None, total=None, percent=None, exception=str(exc)
                )
            )
    return ret


@dataclass
class DiskIO:
    name: str
    read: float
    write: float


_last_disk_io = (time.time(), psutil.disk_io_counters(perdisk=True))


def disk_io() -> list[DiskIO]:
    global _last_disk_io
    try:
        now = time.time()
        past_t, past = _last_disk_io
        now_c = psutil.disk_io_counters(perdisk=True) or {}
        dt = max(1e-6, now - past_t)
        ret: list[DiskIO] = []
        for name, now_one in now_c.items():
            if name not in past:
                continue
            past_one = past[name]
            read = max(0.0, (now_one.read_bytes - past_one.read_bytes) / dt)
            write = max(0.0, (now_one.write_bytes - past_one.write_bytes) / dt)
            ret.append(DiskIO(name=name, read=read, write=write))
        _last_disk_io = (now, now_c)
        ret.sort(key=lambda x: (x.read + x.write), reverse=True)
        return ret[:6]
    except Exception:
        return []


@dataclass
class NetIO:
    name: str
    sent: float
    recv: float


_last_net_io = (time.time(), psutil.net_io_counters(pernic=True))


def network_io(ignore_names: list[str] | None = None) -> list[NetIO]:
    global _last_net_io
    ignore_names = ignore_names or ["lo", "Loopback*"]
    try:
        now = time.time()
        past_t, past = _last_net_io
        now_c = psutil.net_io_counters(pernic=True) or {}
        dt = max(1e-6, now - past_t)
        ret: list[NetIO] = []
        for name, now_one in now_c.items():
            if any(Path(name).match(pat) for pat in ignore_names):
                continue
            if name not in past:
                continue
            past_one = past[name]
            sent = max(0.0, (now_one.bytes_sent - past_one.bytes_sent) / dt)
            recv = max(0.0, (now_one.bytes_recv - past_one.bytes_recv) / dt)
            ret.append(NetIO(name=name, sent=sent, recv=recv))
        _last_net_io = (now, now_c)
        ret.sort(key=lambda x: (x.sent + x.recv), reverse=True)
        return ret[:6]
    except Exception:
        return []


@dataclass
class ConnTest:
    name: str
    status: str
    reason: str
    delay: float
    error: str | None = None


def _normalize_website_targets(raw: Any) -> list[tuple[str, str]]:
    defaults = [
        ("Baidu", "https://www.baidu.com/"),
        ("Google", "https://www.google.com/"),
    ]
    if not isinstance(raw, list) or not raw:
        return defaults
    out: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if url:
                out.append((name or url, url))
        elif isinstance(item, str):
            url = item.strip()
            if url:
                out.append((url, url))
    return out or defaults


async def connection_test(targets: Any = None) -> list[ConnTest]:
    sites = _normalize_website_targets(targets)

    async def probe(name: str, url: str, cli: httpx.AsyncClient) -> ConnTest:
        start = time.perf_counter()
        try:
            resp = await cli.get(url)
            dt = (time.perf_counter() - start) * 1000
            return ConnTest(
                name=name,
                status=str(resp.status_code),
                reason=resp.reason_phrase or "OK",
                delay=dt,
            )
        except Exception as exc:
            dt = (time.perf_counter() - start) * 1000
            return ConnTest(
                name=name,
                status="ERR",
                reason="",
                delay=dt,
                error=f"{exc.__class__.__name__}: {exc}",
            )

    async with httpx.AsyncClient(follow_redirects=True, timeout=5) as cli:
        return await asyncio.gather(*(probe(name, url, cli) for name, url in sites))


@dataclass
class ProcStatus:
    name: str
    pid: int
    cpu: float
    mem: int
    status: str


_BLOCKED_STATUSES = {
    "disk-sleep",
    "wake-kill",
    "waking",
    "locked",
    "waiting",
}
_SLEEPING_STATUSES = {"sleeping", "idle", "parked"}


def process_info(top_n: int = 8) -> dict[str, Any]:
    counts = {
        "all": 0,
        "running": 0,
        "blocked": 0,
        "sleeping": 0,
        "unknown": 0,
    }
    procs: list[ProcStatus] = []
    for p in psutil.process_iter(
        attrs=["pid", "name", "cpu_percent", "memory_info", "status"]
    ):
        try:
            info = p.info
            status = str(info.get("status") or "").lower()
            counts["all"] += 1
            if status == "running":
                counts["running"] += 1
            elif status in _BLOCKED_STATUSES:
                counts["blocked"] += 1
            elif status in _SLEEPING_STATUSES:
                counts["sleeping"] += 1
            else:
                counts["unknown"] += 1

            mem_info = info.get("memory_info")
            mem = int(getattr(mem_info, "rss", 0) or 0)
            cpu = float(info.get("cpu_percent") or 0.0)
            name = str(info.get("name") or info.get("pid") or "?")
            pid = int(info.get("pid") or 0)
            procs.append(
                ProcStatus(
                    name=name, pid=pid, cpu=cpu, mem=mem, status=status
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue

    procs.sort(key=lambda x: (x.mem, x.cpu), reverse=True)
    top = [
        {
            "name": p.name,
            "pid": p.pid,
            "cpu": f"{p.cpu:.1f}%",
            "mem": format_bytes(p.mem),
            "mem_raw": p.mem,
        }
        for p in procs[: max(1, int(top_n))]
    ]
    return {"counts": counts, "top": top}


def _pick_network_interface() -> str:
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        counters = psutil.net_io_counters(pernic=True) or {}
        candidates: list[str] = []
        for name in addrs:
            if name in {"lo", "Loopback Pseudo-Interface 1"}:
                continue
            if not stats.get(name, None) or not stats[name].isup:
                continue
            candidates.append(name)
        if not candidates:
            return "--"

        def score(name: str) -> tuple[int, int]:
            preferred = 1 if name.startswith(
                ("eth", "enp", "ens", "eno", "wlan", "wl")
            ) else 0
            c = counters.get(name)
            traffic = (int(c.bytes_sent) + int(c.bytes_recv)) if c else 0
            return (preferred, traffic)

        return max(candidates, key=score)
    except Exception:
        return "--"


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip().strip('"')
    except Exception:
        pass
    return data


def _os_name() -> str:
    rel = _read_os_release()
    if rel.get("PRETTY_NAME"):
        return rel["PRETTY_NAME"]
    if rel.get("NAME"):
        return f"{rel.get('NAME')} {rel.get('VERSION', '')}".strip()
    if platform.system() == "Windows":
        return f"Windows {platform.release()}"
    return platform.platform()


def _shell_name() -> str:
    shell = os.environ.get("SHELL", "")
    if not shell and pwd is not None:
        try:
            shell = pwd.getpwuid(os.getuid()).pw_shell
        except Exception:
            shell = ""
    return Path(shell).name if shell else "--"


def _run_command(args: list[str], timeout: float = 4.0) -> str | None:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="ignore",
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


_pkg_cache: dict[str, Any] = {"ts": 0.0, "dpkg": None, "pip": None, "gpu": None}


def _package_counts() -> tuple[int | None, int | None]:
    now = time.time()
    if now - float(_pkg_cache.get("ts") or 0) < 3600:
        return _pkg_cache.get("dpkg"), _pkg_cache.get("pip")

    dpkg: int | None = None
    pip: int | None = None
    if shutil.which("dpkg-query"):
        out = _run_command(
            ["dpkg-query", "-W", "-f", "${Package}\n"], timeout=6
        )
        if out is not None:
            dpkg = len([line for line in out.splitlines() if line.strip()])
    try:
        out = _run_command(
            [sys.executable, "-m", "pip", "list", "--format=freeze"], timeout=8
        )
        if out is not None:
            pip = len([line for line in out.splitlines() if line.strip()])
    except Exception:
        pass
    _pkg_cache.update(ts=now, dpkg=dpkg, pip=pip)
    return dpkg, pip


def _gpu_name() -> str:
    now = time.time()
    cached = _pkg_cache.get("gpu")
    if now - float(_pkg_cache.get("ts") or 0) < 3600 and cached:
        return str(cached)
    gpu = "--"
    if shutil.which("lspci"):
        out = _run_command(["lspci"], timeout=4)
        if out:
            for line in out.splitlines():
                low = line.lower()
                if any(key in low for key in ("vga", "3d controller", "display")):
                    gpu = line.split(":", 1)[-1].strip()
                    break
    if gpu == "--" and platform.system() == "Windows":
        out = _run_command(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_VideoController | Select-Object -First 1 -ExpandProperty Name)",
            ],
            timeout=6,
        )
        if out and out.strip():
            gpu = out.strip()
    _pkg_cache["gpu"] = gpu
    return gpu


# ---------------------------------------------------------------------------
# OneBot information (best effort, cached)
# ---------------------------------------------------------------------------

_bot_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_sent_cache: dict[str, Any] = {"ts": 0.0, "value": None}


def _normalize_list(result: Any) -> list[dict[str, Any]] | None:
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return [x for x in data["list"] if isinstance(x, dict)]
    return None


def _normalize_object(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            return data
        return result
    return {}


def _get_aiocqhttp_client(context: Any) -> Any:
    if context is None:
        return None
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
            AiocqhttpAdapter,
        )

        for inst in context.platform_manager.platform_insts:
            if isinstance(inst, AiocqhttpAdapter):
                return inst.get_client()
    except Exception:
        return None
    return None


async def _call_action(client: Any, action: str, timeout: float = 5.0) -> Any:
    return await asyncio.wait_for(client.call_action(action), timeout=timeout)


def query_sent_total() -> int | None:
    """Return the lifetime outgoing message count from platform_stats.

    AstrBot increments platform_stats on send paths only, so this is a true
    "sent" counter. The database is opened read-only.
    """

    now = time.time()
    if now - float(_sent_cache.get("ts") or 0) < 60:
        return _sent_cache.get("value")
    value: int | None = None
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        db_path = Path(get_astrbot_data_path()) / "data_v4.db"
        if db_path.is_file():
            uri = f"file:{db_path.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                row = con.execute(
                    "select coalesce(sum(count), 0) from platform_stats"
                ).fetchone()
                value = int(row[0]) if row else None
            finally:
                con.close()
    except Exception as exc:
        logger.debug("读取 platform_stats 失败: %s", exc)
    _sent_cache.update(ts=now, value=value)
    return value


async def collect_bot_info(
    context: Any,
    event: Any,
    config: Any,
    recv_state: dict[str, Any],
) -> dict[str, Any]:
    now = time.time()
    cache = _bot_cache
    cached = cache.get("data") if now - float(cache.get("ts") or 0) < 600 else None
    if not cached:
        client = _get_aiocqhttp_client(context)
        data: dict[str, Any] = {
            "self_id": "",
            "nick": "",
            "friends": None,
            "groups": None,
            "members": None,
            "protocol_name": "",
            "protocol_version": "",
            "online": None,
            "has_client": client is not None,
        }
        if client is not None:
            try:
                login = _normalize_object(await _call_action(client, "get_login_info"))
                data["self_id"] = str(login.get("user_id") or "")
                data["nick"] = str(login.get("nickname") or "")
            except Exception:
                pass
            try:
                version = _normalize_object(await _call_action(client, "get_version_info"))
                data["protocol_name"] = str(
                    version.get("app_name")
                    or version.get("app_full_name")
                    or "NapCat.Onebot"
                )
                data["protocol_version"] = str(
                    version.get("app_version")
                    or version.get("version")
                    or ""
                )
            except Exception:
                pass
            try:
                friends = _normalize_list(await _call_action(client, "get_friend_list"))
                if friends is not None:
                    data["friends"] = len(friends)
            except Exception:
                pass
            try:
                groups = _normalize_list(await _call_action(client, "get_group_list"))
                if groups is not None:
                    data["groups"] = len(groups)
                    data["members"] = sum(
                        int(g.get("member_count") or 0) for g in groups
                    )
            except Exception:
                pass
            try:
                status = _normalize_object(await _call_action(client, "get_status"))
                if "online" in status:
                    data["online"] = bool(status.get("online"))
            except Exception:
                pass

        cache.update(ts=now, data=data)
        cached = data

    bot = dict(cached or {})

    try:
        self_id = event.get_self_id() if event is not None else ""
    except Exception:
        self_id = ""
    if self_id and str(self_id).isdigit():
        bot["self_id"] = str(self_id)

    try:
        event_adapter = event.get_platform_name() if event is not None else ""
    except Exception:
        event_adapter = ""
    bot["adapter"] = (
        "aiocqhttp"
        if bot.get("has_client")
        else (event_adapter or "aiocqhttp")
    )

    nick_cfg = cfg_get(config, "avatar.avatar_text", "")
    nick_cfg = nick_cfg.strip() if isinstance(nick_cfg, str) else ""
    bot["nick"] = nick_cfg or bot.get("nick") or "AstrBot"

    bot["astrbot_version"] = resolve_astrbot_version(context)
    bot["run_time"] = format_duration(_dt_now().timestamp() - PROCESS_START_TIME.timestamp())
    bot["online_text"] = (
        "在线" if bot.get("online") is True else "离线" if bot.get("online") is False else "--"
    )

    recv_available = bool(recv_state.get("available"))
    baseline = int(recv_state.get("baseline") or 0)
    accum = int(recv_state.get("accum") or 0)
    bot["recv"] = (baseline + accum) if recv_available else None
    bot["recv_scope"] = "message_stats 跟踪群" if recv_available else "暂无接收统计"
    bot["sent"] = query_sent_total()
    bot["sent_scope"] = "platform_stats 全量"

    bot["avatar_url"] = ""
    if str(bot.get("self_id") or "").isdigit():
        bot["avatar_url"] = (
            f"https://q1.qlogo.cn/g?b=qq&nk={bot['self_id']}&s=640"
        )
    return bot


async def fetch_avatar_bytes(bot: dict[str, Any], config: Any) -> bytes | None:
    local_path = cfg_get(config, "avatar.avatar_local_path", "")
    if isinstance(local_path, str) and local_path.strip():
        try:
            return Path(local_path.strip()).read_bytes()
        except Exception as exc:
            logger.warning("读取本地头像失败 %s: %s", local_path, exc)

    url = cfg_get(config, "avatar.avatar_url", "")
    if not (isinstance(url, str) and url.strip()):
        url = bot.get("avatar_url") or ""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=5) as cli:
            resp = await cli.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        logger.warning("获取头像失败 %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Network chart
# ---------------------------------------------------------------------------


def _nice_ceil(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = value / (10**exp)
    for step in (1, 2, 5, 10):
        if base <= step:
            return step * (10**exp)
    return 10 ** (exp + 1)


def build_network_chart(history: Any) -> dict[str, Any]:
    width = 560
    height = 150
    left = 54
    right = 10
    top = 12
    bottom = 24
    plot_w = width - left - right
    plot_h = height - top - bottom

    points: list[tuple[float, float, float]] = []
    if isinstance(history, list):
        for item in history[-60:]:
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    points.append((float(item[0]), float(item[1]), float(item[2])))
            except Exception:
                continue

    if not points:
        return {
            "width": width,
            "height": height,
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "plot_w": plot_w,
            "plot_h": plot_h,
            "up_points": "",
            "down_points": "",
            "up_area": "",
            "down_area": "",
            "y_labels": [],
            "x_labels": [],
            "peak_up": None,
            "peak_down": None,
            "empty": True,
        }

    # Pad the left side with zero samples so a freshly started plugin still
    # renders a full one-hour chart instead of a two-point diagonal.
    if len(points) < 60:
        first_ts = points[0][0]
        pad = 60 - len(points)
        points = [
            (first_ts - (pad - i) * 60, 0.0, 0.0) for i in range(pad)
        ] + points

    max_value = max(max(p[1], p[2]) for p in points)
    max_value = _nice_ceil(max_value * 1.12)
    n = len(points)
    denom = max(1, n - 1)

    def xy(index: int, value: float) -> tuple[float, float]:
        x = left + plot_w * (index / denom)
        y = top + plot_h * (1 - min(1.0, value / max_value))
        return round(x, 1), round(y, 1)

    up_coords = [xy(i, p[1]) for i, p in enumerate(points)]
    down_coords = [xy(i, p[2]) for i, p in enumerate(points)]
    up_points = " ".join(f"{x},{y}" for x, y in up_coords)
    down_points = " ".join(f"{x},{y}" for x, y in down_coords)
    bottom_y = top + plot_h
    up_area = (
        f"M {up_coords[0][0]},{bottom_y} "
        + " ".join(f"L {x},{y}" for x, y in up_coords)
        + f" L {up_coords[-1][0]},{bottom_y} Z"
    )
    down_area = (
        f"M {down_coords[0][0]},{bottom_y} "
        + " ".join(f"L {x},{y}" for x, y in down_coords)
        + f" L {down_coords[-1][0]},{bottom_y} Z"
    )

    y_labels = []
    for i in range(5):
        value = max_value * i / 4
        y = top + plot_h * (1 - i / 4)
        y_labels.append({"y": round(y, 1), "text": format_bytes(value, 1)})

    x_labels = []
    for i in range(5):
        idx = round((n - 1) * i / 4)
        ts = points[idx][0]
        x = left + plot_w * (idx / denom)
        x_labels.append(
            {
                "x": round(x, 1),
                "text": datetime.fromtimestamp(ts).strftime("%H:%M"),
            }
        )

    up_peak_idx = max(range(n), key=lambda i: points[i][1])
    down_peak_idx = max(range(n), key=lambda i: points[i][2])
    peak_up = None
    peak_down = None
    if points[up_peak_idx][1] > 0:
        x, y = up_coords[up_peak_idx]
        peak_up = {"x": x, "y": y, "text": format_speed(points[up_peak_idx][1])}
    if points[down_peak_idx][2] > 0:
        x, y = down_coords[down_peak_idx]
        peak_down = {"x": x, "y": y, "text": format_speed(points[down_peak_idx][2])}

    return {
        "width": width,
        "height": height,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "plot_w": plot_w,
        "plot_h": plot_h,
        "up_points": up_points,
        "down_points": down_points,
        "up_area": up_area,
        "down_area": down_area,
        "y_labels": y_labels,
        "x_labels": x_labels,
        "peak_up": peak_up,
        "peak_down": peak_down,
        "empty": False,
    }


# ---------------------------------------------------------------------------
# Main collection
# ---------------------------------------------------------------------------


def _overview_cards(
    context: Any, bot: dict[str, Any]
) -> list[dict[str, str]]:
    boot = BOOT_TIME
    uptime = format_duration(_dt_now().timestamp() - boot.timestamp())
    plugin_count = 0
    try:
        if context is not None:
            stars = context.get_all_stars()
            plugin_count = len(
                [
                    star
                    for star in stars
                    if getattr(star, "name", "")
                    not in {"astrbot", "builtin_commands"}
                ]
            )
    except Exception:
        pass
    host = socket.gethostname()
    os_name = _os_name()
    cores = cpu_count() or cpu_count_logical() or 0
    return [
        {
            "icon": "🖥",
            "label": "系统",
            "value": os_name,
            "sub": f"{platform.system()} {platform.release()} / {platform.machine()}",
        },
        {
            "icon": "🏠",
            "label": "主机名",
            "value": host,
            "sub": f"{cores} Cores",
        },
        {
            "icon": "⏱",
            "label": "服务器已运行",
            "value": uptime,
            "sub": f"启动于 {boot.strftime('%Y-%m-%d %H:%M:%S')}",
        },
        {
            "icon": "🧩",
            "label": "插件数",
            "value": f"{plugin_count} plugins",
            "sub": "AstrBot",
        },
    ]


def _system_info_rows(
    context: Any,
    bot: dict[str, Any],
    disks: list[DiskUsage],
    net_interface: str,
) -> list[dict[str, str]]:
    dpkg, pip = _package_counts()
    packages = (
        f"{pip if pip is not None else '--'} pip / {dpkg if dpkg is not None else '--'} dpkg"
    )
    disk_text = "--"
    if disks:
        d = disks[0]
        if d.used is not None and d.total is not None:
            disk_text = f"{format_bytes(d.used)} / {format_bytes(d.total)}"
    mem = memory_stat()
    cpu_brand = get_cpu_brand()
    cores = cpu_count() or cpu_count_logical() or 0
    runtime = readable_python_version()
    protocol = " ".join(
        x for x in (bot.get("protocol_name"), bot.get("protocol_version")) if x
    ) or bot.get("adapter", "--")
    return [
        {"label": "OS", "value": _os_name()},
        {"label": "Host", "value": socket.gethostname()},
        {"label": "Kernel", "value": platform.release()},
        {"label": "Boot", "value": BOOT_TIME.strftime("%Y-%m-%d %H:%M:%S")},
        {"label": "Runtime", "value": runtime},
        {"label": "Packages", "value": packages},
        {"label": "Shell", "value": _shell_name()},
        {"label": "Terminal", "value": "AstrBot T2I"},
        {"label": "CPU", "value": f"{cpu_brand} ({cores} Cores)"},
        {"label": "GPU", "value": _gpu_name()},
        {
            "label": "Memory",
            "value": f"{format_bytes(mem.used)} / {format_bytes(mem.total)}",
        },
        {"label": "Network", "value": net_interface},
        {"label": "Arch", "value": platform.machine()},
        {"label": "Scope", "value": "宿主机" if platform.system() == "Linux" else "本机"},
        {"label": "Disk", "value": disk_text},
        {"label": "Platform", "value": protocol},
    ]


def _resources(
    cpu_pct: float, mem: MemStat, swap: MemStat
) -> list[dict[str, Any]]:
    brand = get_cpu_brand()
    cores = cpu_count() or cpu_count_logical() or 0
    logical = cpu_count_logical() or cores
    freq = cpu_freq()
    freq_text = "--"
    if freq.current:
        freq_text = (
            f"{freq.current / 1000:.2f}GHz"
            if freq.current >= 1000
            else f"{freq.current:.0f}MHz"
        )
    return [
        {
            "percent": cpu_pct,
            "title": "CPU",
            "desc": f"{freq_text} | {cores}核 {logical}线程\n{brand}",
        },
        {
            "percent": mem.percent,
            "title": "RAM",
            "desc": f"{format_bytes(mem.used)} / {format_bytes(mem.total)}\n物理内存使用",
        },
        {
            "percent": swap.percent,
            "title": "SWAP",
            "desc": f"{format_bytes(swap.used)} / {format_bytes(swap.total)}\n交换空间使用",
        },
    ]


async def collect_all(
    context: Any = None,
    event: Any = None,
    state: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    now = _dt_now()
    cpu_pct = cpu_percent()
    mem = memory_stat()
    swap = swap_stat()
    disk_paths = cfg_get(config, "display.disk_paths", None)
    if not isinstance(disk_paths, list) or not disk_paths:
        disk_paths = _default_disk_paths()
    disks = disk_usage([str(x) for x in disk_paths])
    if disks and all(d.exception for d in disks):
        disks = disk_usage(_default_disk_paths())
    net_interface = _pick_network_interface()

    sites_cfg = cfg_get(config, "display.website_targets", None)
    websites = await connection_test(sites_cfg)

    top_n = cfg_int(config, "display.process_top_n", 8)
    proc = process_info(top_n)

    snapshot = state.snapshot() if hasattr(state, "snapshot") else {}
    net_state = snapshot.get("net", {}) if isinstance(snapshot, dict) else {}
    disk_state = snapshot.get("disk_io", {}) if isinstance(snapshot, dict) else {}
    recv_state = snapshot.get("recv", {}) if isinstance(snapshot, dict) else {}
    if not net_state and state is not None and hasattr(state, "get"):
        net_state = state.get("net", {}) or {}
    if not disk_state and state is not None and hasattr(state, "get"):
        disk_state = state.get("disk_io", {}) or {}
    if not recv_state and state is not None and hasattr(state, "get"):
        recv_state = state.get("recv", {}) or {}

    bot = await collect_bot_info(context, event, config, recv_state)

    net_counters = psutil.net_io_counters()
    history = net_state.get("history") if isinstance(net_state, dict) else []
    chart = build_network_chart(history)
    up_rate = float(net_state.get("up_rate") or 0.0)
    down_rate = float(net_state.get("down_rate") or 0.0)
    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    month = str(net_state.get("month") or now.strftime("%Y-%m"))
    try:
        month_label = month_names[int(month.split("-")[1])]
    except Exception:
        month_label = month

    disk_read = float(disk_state.get("read_rate") or 0.0)
    disk_write = float(disk_state.get("write_rate") or 0.0)
    if disk_read == 0.0 and disk_write == 0.0:
        d_io = disk_io()
        if d_io:
            disk_read = sum(x.read for x in d_io)
            disk_write = sum(x.write for x in d_io)

    boot_uptime = format_duration(now.timestamp() - BOOT_TIME.timestamp())
    overview = _overview_cards(context, bot)
    system_info = await asyncio.to_thread(
        _system_info_rows, context, bot, disks, net_interface
    )

    view = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "bot": bot,
        "overview": overview,
        "resources": _resources(cpu_pct, mem, swap),
        "disks": [
            {
                "path": d.name,
                "used": format_bytes(d.used),
                "total": format_bytes(d.total),
                "percent": d.percent if d.percent is not None else 0.0,
                "percent_text": (
                    f"{d.percent:.1f}%" if d.percent is not None else "--"
                ),
                "error": d.exception,
            }
            for d in disks
        ],
        "disk_io": {
            "read": format_speed(disk_read),
            "write": format_speed(disk_write),
        },
        "network": {
            "up": format_speed(up_rate),
            "down": format_speed(down_rate),
            "up_total": format_bytes(net_counters.bytes_sent),
            "down_total": format_bytes(net_counters.bytes_recv),
            "month": month_label,
            "month_up": format_bytes(net_state.get("month_up") or 0),
            "month_down": format_bytes(net_state.get("month_down") or 0),
            "chart": chart,
            "interface": net_interface,
        },
        "websites": [
            {
                "name": x.name,
                "status": x.status,
                "delay": (
                    f"{x.delay:.0f}ms" if x.delay < 1000 else f"{x.delay / 1000:.2f}s"
                ),
                "ok": not x.error,
                "error": x.error,
            }
            for x in websites
        ],
        "process": proc,
        "system_info": system_info,
        "uptime": boot_uptime,
        "footer": (
            f"Created for AstrBot · AstrBot {bot.get('astrbot_version', '')} "
            f"· {now.strftime('%Y-%m-%d %H:%M:%S')}"
        ),
        "background_data_url": "",
        "avatar_data_url": "",
    }
    return {"view": view}
