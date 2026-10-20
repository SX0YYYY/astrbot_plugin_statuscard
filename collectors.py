from __future__ import annotations

import asyncio
import math
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time

try:
    import pwd
except ImportError:  # Windows development environment
    pwd = None  # type: ignore[assignment]
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psutil
from astrbot.api import logger

from .utils import CpuFreq, readable_python_version
from .version_resolver import resolve_astrbot_version

MAX_IMAGE_BYTES = 10 * 1024 * 1024
_TZ_WARNED: set[str] = set()


def _dt_now() -> datetime:
    return datetime.now(UTC).astimezone()


def get_timezone(config: Any = None):
    name = str(cfg_get(config, "display.timezone", "Asia/Shanghai") or "Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except Exception:
        if name not in _TZ_WARNED:
            logger.warning("时区配置非法：%s，回退到系统时区", name)
            _TZ_WARNED.add(name)
        return datetime.now().astimezone().tzinfo


def now_in_timezone(config: Any = None) -> datetime:
    return datetime.now(get_timezone(config))


BOOT_TIME = datetime.fromtimestamp(psutil.boot_time(), tz=UTC).astimezone()
PROCESS_START_TIME = datetime.fromtimestamp(
    psutil.Process(os.getpid()).create_time(), tz=UTC
).astimezone()


def format_bytes(value: float | None, digits: int = 2) -> str:
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


def format_speed(value: float | None) -> str:
    return f"{format_bytes(value, 1)}/s"


def format_duration(seconds: float | None) -> str:
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
        if hasattr(cur, "get") or isinstance(cur, dict):
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


_SYSTEM_CPU_LOCK = threading.Lock()
_SYSTEM_CPU_INIT_LOCK = threading.Lock()
_SYSTEM_CPU_STATE: dict[str, float] = {"ts": 0.0, "percent": 0.0}


def sample_system_cpu() -> float:
    """Sample system CPU and cache the result.

    ``psutil.cpu_percent(None)`` measures the interval since the previous
    call.  It must be called from one place only, otherwise status cards and
    the alert loop steal each other's sampling window.
    """

    with _SYSTEM_CPU_INIT_LOCK:
        value = float(psutil.cpu_percent(interval=None) or 0.0)
        with _SYSTEM_CPU_LOCK:
            _SYSTEM_CPU_STATE["ts"] = time.monotonic()
            _SYSTEM_CPU_STATE["percent"] = value
        return value


def ensure_system_cpu() -> float:
    """Return a seeded CPU value, taking a short first sample if needed."""

    with _SYSTEM_CPU_LOCK:
        if _SYSTEM_CPU_STATE["ts"] > 0:
            return float(_SYSTEM_CPU_STATE["percent"])
    with _SYSTEM_CPU_INIT_LOCK:
        with _SYSTEM_CPU_LOCK:
            if _SYSTEM_CPU_STATE["ts"] > 0:
                return float(_SYSTEM_CPU_STATE["percent"])
        psutil.cpu_percent(interval=None)
        time.sleep(0.3)
        value = float(psutil.cpu_percent(interval=None) or 0.0)
        with _SYSTEM_CPU_LOCK:
            _SYSTEM_CPU_STATE["ts"] = time.monotonic()
            _SYSTEM_CPU_STATE["percent"] = value
        return value


def cached_cpu_percent() -> float:
    with _SYSTEM_CPU_LOCK:
        return float(_SYSTEM_CPU_STATE["percent"] or 0.0)


def cpu_percent() -> float:
    with _SYSTEM_CPU_LOCK:
        if _SYSTEM_CPU_STATE["ts"] > 0:
            return float(_SYSTEM_CPU_STATE["percent"])
    return ensure_system_cpu()


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
        ret.sort(key=lambda x: x.read + x.write, reverse=True)
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
        ret.sort(key=lambda x: x.sent + x.recv, reverse=True)
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


@dataclass(frozen=True)
class WebsiteTarget:
    name: str
    url: str
    timeout: float = 3.0


_WEBSITE_CACHE_LOCK = asyncio.Lock()
_WEBSITE_CACHE: dict[
    tuple[tuple[str, str, float], ...], tuple[float, list[ConnTest]]
] = {}


def _normalize_website_targets(raw: Any) -> list[WebsiteTarget]:
    defaults = [WebsiteTarget("Baidu", "https://www.baidu.com/", 3.0)]
    if not isinstance(raw, list) or not raw:
        return defaults
    out: list[WebsiteTarget] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            try:
                timeout = float(item.get("timeout_seconds") or 3.0)
            except (TypeError, ValueError):
                timeout = 3.0
            timeout = min(30.0, max(1.0, timeout))
            out.append(WebsiteTarget(name or url, url, timeout))
        elif isinstance(item, str):
            url = item.strip()
            if url:
                out.append(WebsiteTarget(url, url, 3.0))
    return out or defaults


def _copy_conn_tests(results: list[ConnTest]) -> list[ConnTest]:
    return [
        ConnTest(
            name=x.name,
            status=x.status,
            reason=x.reason,
            delay=x.delay,
            error=x.error,
        )
        for x in results
    ]


async def _probe_websites(sites: list[WebsiteTarget]) -> list[ConnTest]:
    async def probe(site: WebsiteTarget, cli: httpx.AsyncClient) -> ConnTest:
        start = time.perf_counter()
        try:
            resp = await cli.get(site.url, timeout=site.timeout)
            dt = (time.perf_counter() - start) * 1000
            return ConnTest(
                name=site.name,
                status=str(resp.status_code),
                reason=resp.reason_phrase or "OK",
                delay=dt,
            )
        except Exception as exc:
            dt = (time.perf_counter() - start) * 1000
            return ConnTest(
                name=site.name,
                status="ERR",
                reason="",
                delay=dt,
                error=f"{exc.__class__.__name__}: {exc}",
            )

    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as cli:
        return await asyncio.gather(*(probe(site, cli) for site in sites))


async def connection_test(
    targets: Any = None,
    cache_seconds: float = 60.0,
) -> list[ConnTest]:
    sites = _normalize_website_targets(targets)
    try:
        cache_seconds = max(0.0, min(3600.0, float(cache_seconds or 0.0)))
    except (TypeError, ValueError):
        cache_seconds = 60.0
    if cache_seconds <= 0:
        return await _probe_websites(sites)

    key = tuple((site.name, site.url, site.timeout) for site in sites)
    now = time.monotonic()
    cached = _WEBSITE_CACHE.get(key)
    if cached and now - cached[0] < cache_seconds:
        return _copy_conn_tests(cached[1])

    async with _WEBSITE_CACHE_LOCK:
        cached = _WEBSITE_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < cache_seconds:
            return _copy_conn_tests(cached[1])
        results = await _probe_websites(sites)
        _WEBSITE_CACHE[key] = (time.monotonic(), results)
        return _copy_conn_tests(results)


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

_PROC_CPU_LOCK = threading.Lock()
_PROC_CPU_PREV: dict[tuple[int, float], tuple[float, float, float]] = {}
_PROC_CPU_READY = False


def _process_key(pid: Any, create_time: Any) -> tuple[int, float]:
    try:
        pid_value = int(pid or 0)
    except (TypeError, ValueError):
        pid_value = 0
    try:
        created_value = float(create_time or 0.0)
    except (TypeError, ValueError):
        created_value = 0.0
    return pid_value, created_value


def _process_cpu_total(cpu_times: Any) -> float:
    if cpu_times is None:
        return 0.0
    try:
        return float(getattr(cpu_times, "user", 0.0) or 0.0) + float(
            getattr(cpu_times, "system", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        return 0.0


def prime_process_cpu() -> None:
    """Seed or refresh the per-process CPU snapshot.

    ``psutil.Process.cpu_percent()`` returns 0.0 on its first call, so the
    card would otherwise always show zeros.  We keep our own wall-clock
    snapshot and compute deltas between calls instead.
    """

    global _PROC_CPU_READY
    now = time.monotonic()
    with _PROC_CPU_LOCK:
        previous = dict(_PROC_CPU_PREV)
    snapshot: dict[tuple[int, float], tuple[float, float, float]] = {}
    for p in psutil.process_iter(attrs=["pid", "create_time", "cpu_times"]):
        try:
            info = p.info
            cpu_times = info.get("cpu_times")
            if cpu_times is None:
                continue
            key = _process_key(info.get("pid"), info.get("create_time"))
            total_cpu = _process_cpu_total(cpu_times)
            prev = previous.get(key)
            cpu = 0.0
            if prev is not None:
                delta_time = now - prev[0]
                if delta_time >= 0.2:
                    cpu = max(
                        0.0,
                        (total_cpu - prev[1]) / delta_time * 100.0,
                    )
                else:
                    cpu = float(prev[2])
            snapshot[key] = (now, total_cpu, cpu)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    with _PROC_CPU_LOCK:
        _PROC_CPU_PREV.clear()
        _PROC_CPU_PREV.update(snapshot)
        if previous:
            _PROC_CPU_READY = True


def process_info(top_n: int = 8) -> dict[str, Any]:
    global _PROC_CPU_READY
    counts = {
        "all": 0,
        "running": 0,
        "blocked": 0,
        "sleeping": 0,
        "unknown": 0,
    }
    procs: list[ProcStatus] = []
    with _PROC_CPU_LOCK:
        previous = dict(_PROC_CPU_PREV)
        ready = _PROC_CPU_READY
    if not ready:
        # First card after startup: take a quick second sample so the table
        # does not show a full column of 0.0%.
        prime_process_cpu()
        time.sleep(0.5)
        prime_process_cpu()
        with _PROC_CPU_LOCK:
            previous = dict(_PROC_CPU_PREV)
            _PROC_CPU_READY = True

    for p in psutil.process_iter(
        attrs=[
            "pid",
            "name",
            "create_time",
            "memory_info",
            "status",
        ]
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
            name = str(info.get("name") or info.get("pid") or "?")
            pid = int(info.get("pid") or 0)
            key = _process_key(pid, info.get("create_time"))
            entry = previous.get(key)
            cpu = float(entry[2]) if entry is not None else 0.0
            procs.append(
                ProcStatus(name=name, pid=pid, cpu=cpu, mem=mem, status=status)
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue

    procs.sort(key=lambda x: (x.cpu, x.mem), reverse=True)
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
            preferred = (
                1 if name.startswith(("eth", "enp", "ens", "eno", "wlan", "wl")) else 0
            )
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
_SYSINFO_CACHE_TTL = 24 * 3600


def _load_sysinfo_cache(state: Any = None) -> dict[str, Any]:
    if state is None or not hasattr(state, "get"):
        return {}
    value = state.get("sysinfo_cache", {}) or {}
    return value if isinstance(value, dict) else {}


def _save_sysinfo_cache(state: Any, updates: dict[str, Any]) -> None:
    """Update the shared cache without forcing a disk write from a worker thread.

    ``collect_all`` runs the system-info block in ``asyncio.to_thread`` while
    the sampler also saves state. Writing the whole state file from both places
    can race on the temporary file, so this only updates the in-memory dict;
    the next sampler save persists it.
    """

    if state is None or not hasattr(state, "setdefault"):
        return
    cache = state.setdefault("sysinfo_cache", {})
    if not isinstance(cache, dict):
        cache = {}
        state.data["sysinfo_cache"] = cache
    cache.update(updates)


def _package_counts(
    state: Any = None,
) -> tuple[int | None, int | None]:
    now = time.time()
    persisted = _load_sysinfo_cache(state)
    cache_ts = float(_pkg_cache.get("ts") or persisted.get("ts") or 0)
    if now - cache_ts < _SYSINFO_CACHE_TTL:
        return (
            _pkg_cache.get("dpkg", persisted.get("dpkg")),
            _pkg_cache.get("pip", persisted.get("pip")),
        )

    dpkg: int | None = None
    pip: int | None = None
    if shutil.which("dpkg-query"):
        out = _run_command(["dpkg-query", "-W", "-f", "${Package}\n"], timeout=6)
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
    _save_sysinfo_cache(state, {"ts": now, "dpkg": dpkg, "pip": pip})
    return dpkg, pip


def _gpu_name(state: Any = None) -> str:
    now = time.time()
    persisted = _load_sysinfo_cache(state)
    cached = _pkg_cache.get("gpu") or persisted.get("gpu")
    cache_ts = float(_pkg_cache.get("ts") or persisted.get("ts") or 0)
    if now - cache_ts < _SYSINFO_CACHE_TTL and cached and cached != "--":
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
    _save_sysinfo_cache(state, {"ts": now, "gpu": gpu})
    return gpu


# ---------------------------------------------------------------------------
# OneBot information (best effort, cached)
# ---------------------------------------------------------------------------

_bot_cache: dict[str, dict[str, tuple[float, Any]]] = {}
_BOT_CACHE_SUCCESS_TTL = 600.0
_BOT_CACHE_FAILURE_TTL = 30.0
_BOT_CACHE_MAX_KEYS = 64
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


def _get_aiocqhttp_client(
    context: Any,
    platform_id: str | None = None,
    self_id: str | None = None,
) -> Any:
    if context is None:
        return None
    try:
        manager = getattr(context, "platform_manager", None)
        if manager is None:
            return None
        if hasattr(manager, "get_insts"):
            insts = manager.get_insts()
        else:
            insts = getattr(manager, "platform_insts", [])
        for inst in insts:
            try:
                meta = (
                    inst.meta()
                    if callable(getattr(inst, "meta", None))
                    else getattr(inst, "meta", None)
                )
            except Exception:
                meta = None
            name = str(getattr(meta, "name", "") or "")
            inst_id = str(getattr(meta, "id", "") or "")
            if name != "aiocqhttp" and inst.__class__.__name__ != "AiocqhttpAdapter":
                continue
            if platform_id and inst_id and inst_id != str(platform_id):
                continue
            get_client = getattr(inst, "get_client", None)
            if not callable(get_client):
                continue
            client = get_client()
            if client is None:
                continue
            client_self = getattr(client, "self_id", None)
            if client_self is None:
                bot = getattr(client, "bot", None)
                client_self = getattr(bot, "self_id", None)
            if self_id and client_self is not None and str(client_self) != str(self_id):
                continue
            return client
    except Exception as exc:
        logger.debug("获取 OneBot 客户端失败: %s", exc)
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
    platform_id = ""
    self_id = ""
    try:
        if event is not None:
            platform_id = str(event.get_platform_id() or "")
            self_id = str(event.get_self_id() or "")
    except Exception:
        pass
    if not platform_id:
        platform_id = str(cfg_get(config, "alerts.platform_id", "default") or "default")
    cache_key = f"{platform_id}:{self_id or 'default'}"
    cache = _bot_cache.setdefault(cache_key, {})
    if len(_bot_cache) > _BOT_CACHE_MAX_KEYS:
        oldest = min(
            _bot_cache,
            key=lambda key: max(
                (item[0] for item in _bot_cache[key].values()),
                default=0.0,
            ),
        )
        if oldest != cache_key:
            _bot_cache.pop(oldest, None)

    def cached(name: str) -> tuple[bool, Any]:
        item = cache.get(name)
        if not item:
            return False, None
        ts, value = item
        ttl = _BOT_CACHE_SUCCESS_TTL if value is not None else _BOT_CACHE_FAILURE_TTL
        return (now - ts < ttl), value

    def store(name: str, value: Any) -> None:
        cache[name] = (now, value)

    client = _get_aiocqhttp_client(context, platform_id, self_id)
    data: dict[str, Any] = {
        "self_id": "",
        "nick": "",
        "friends": None,
        "groups": None,
        "members": None,
        "protocol_name": "",
        "protocol_version": "",
        "online": None,
    }
    for field in (
        "self_id",
        "nick",
        "protocol_name",
        "protocol_version",
        "friends",
        "groups",
        "members",
        "online",
    ):
        ok, value = cached(field)
        if ok:
            data[field] = value

    if client is not None:
        # The five OneBot calls used to run serially, so a slow NapCat could
        # add 5 seconds per field to the first card. They are independent, so
        # run them concurrently and let each field keep its own cache state.
        async def fetch_login() -> None:
            ok_self, _ = cached("self_id")
            ok_nick, _ = cached("nick")
            if ok_self and ok_nick:
                return
            try:
                login = _normalize_object(await _call_action(client, "get_login_info"))
                sid = str(login.get("user_id") or "")
                nick = str(login.get("nickname") or "")
                data["self_id"] = sid
                data["nick"] = nick
                store("self_id", sid)
                store("nick", nick)
            except Exception as exc:
                logger.debug("OneBot get_login_info 失败: %s", exc)
                store("self_id", None)
                store("nick", None)

        async def fetch_version() -> None:
            ok_pname, _ = cached("protocol_name")
            ok_pver, _ = cached("protocol_version")
            if ok_pname and ok_pver:
                return
            try:
                version = _normalize_object(
                    await _call_action(client, "get_version_info")
                )
                pname = str(
                    version.get("app_name")
                    or version.get("app_full_name")
                    or "NapCat.Onebot"
                )
                pver = str(version.get("app_version") or version.get("version") or "")
                data["protocol_name"] = pname
                data["protocol_version"] = pver
                store("protocol_name", pname)
                store("protocol_version", pver)
            except Exception as exc:
                logger.debug("OneBot get_version_info 失败: %s", exc)
                store("protocol_name", None)
                store("protocol_version", None)

        async def fetch_friends() -> None:
            ok_friends, _ = cached("friends")
            if ok_friends:
                return
            try:
                friends = _normalize_list(await _call_action(client, "get_friend_list"))
                value = len(friends) if friends is not None else None
                data["friends"] = value
                store("friends", value)
            except Exception as exc:
                logger.debug("OneBot get_friend_list 失败: %s", exc)
                store("friends", None)

        async def fetch_groups() -> None:
            ok_groups, _ = cached("groups")
            ok_members, _ = cached("members")
            if ok_groups and ok_members:
                return
            try:
                groups = _normalize_list(await _call_action(client, "get_group_list"))
                if groups is not None:
                    gcount = len(groups)
                    mcount = sum(int(g.get("member_count") or 0) for g in groups)
                    data["groups"] = gcount
                    data["members"] = mcount
                    store("groups", gcount)
                    store("members", mcount)
                else:
                    store("groups", None)
                    store("members", None)
            except Exception as exc:
                logger.debug("OneBot get_group_list 失败: %s", exc)
                store("groups", None)
                store("members", None)

        async def fetch_online() -> None:
            ok_online, _ = cached("online")
            if ok_online:
                return
            try:
                status = _normalize_object(await _call_action(client, "get_status"))
                online = bool(status.get("online")) if "online" in status else True
                data["online"] = online
                store("online", online)
            except Exception as exc:
                logger.debug("OneBot get_status 失败: %s", exc)
                store("online", None)

        await asyncio.gather(
            fetch_login(),
            fetch_version(),
            fetch_friends(),
            fetch_groups(),
            fetch_online(),
            return_exceptions=True,
        )

    data["has_client"] = client is not None
    bot = dict(data)

    try:
        event_self_id = event.get_self_id() if event is not None else ""
    except Exception:
        event_self_id = ""
    if event_self_id and str(event_self_id).isdigit():
        bot["self_id"] = str(event_self_id)

    try:
        event_adapter = event.get_platform_name() if event is not None else ""
    except Exception:
        event_adapter = ""
    bot["adapter"] = (
        "aiocqhttp" if bot.get("has_client") else (event_adapter or "aiocqhttp")
    )

    nick_cfg = cfg_get(config, "avatar.avatar_text", "")
    nick_cfg = nick_cfg.strip() if isinstance(nick_cfg, str) else ""
    bot["nick"] = nick_cfg or bot.get("nick") or "AstrBot"

    bot["astrbot_version"] = resolve_astrbot_version(context)
    bot["run_time"] = format_duration(
        _dt_now().timestamp() - PROCESS_START_TIME.timestamp()
    )
    bot["online_text"] = (
        "在线"
        if bot.get("online") is True
        else "离线"
        if bot.get("online") is False
        else "--"
    )

    recv_available = bool(recv_state.get("available"))
    baseline = int(recv_state.get("baseline") or 0)
    accum = int(recv_state.get("accum") or 0)
    bot["recv"] = (baseline + accum) if recv_available else None
    bot["recv_scope"] = "message_stats 跟踪群" if recv_available else "暂无接收统计"
    bot["sent"] = await asyncio.to_thread(query_sent_total)
    bot["sent_scope"] = "platform_stats 全量"

    bot["avatar_url"] = ""
    if str(bot.get("self_id") or "").isdigit():
        bot["avatar_url"] = f"https://q1.qlogo.cn/g?b=qq&nk={bot['self_id']}&s=640"
    return bot


def uploaded_file_path(config: Any, key: str, data_dir: Any) -> Path | None:
    """Return the first existing file uploaded through the WebUI config page.

    AstrBot stores ``file`` config items under
    ``data/plugin_data/<plugin>/files/<config key>/`` and keeps a relative
    path such as ``files/avatar/avatar_upload_files/avatar.png`` in the
    configuration. Only paths that stay inside the plugin data directory are
    accepted.
    """

    raw = cfg_get(config, key, [])
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [item for item in raw if isinstance(item, str)]
    else:
        return None
    if data_dir is None:
        return None
    try:
        base = Path(data_dir).resolve()
    except Exception:
        return None
    for value in values:
        rel = value.strip().replace("\\", "/").lstrip("/")
        if not rel or not rel.startswith("files/"):
            continue
        try:
            target = (base / rel).resolve()
            target.relative_to(base)
        except (OSError, ValueError):
            continue
        if target.is_file():
            return target
    return None


def effective_media_source(
    config: Any, source_key: str, upload_key: str, data_dir: Any
) -> str:
    """Return ``default`` or ``upload`` for an avatar/background setting.

    Older configurations had no ``*_source`` field. When such a config still
    contains uploaded files, treat it as ``upload`` so the upgrade does not
    silently drop the user's image.
    """

    source = str(cfg_get(config, source_key, "") or "").strip().lower()
    if source in {"default", "upload"}:
        return source
    return (
        "upload"
        if uploaded_file_path(config, upload_key, data_dir) is not None
        else "default"
    )


async def fetch_avatar_bytes(
    bot: dict[str, Any], config: Any, data_dir: Any = None
) -> bytes | None:
    source = effective_media_source(
        config,
        "avatar.avatar_source",
        "avatar.avatar_upload_files",
        data_dir,
    )
    if source == "upload":
        uploaded = uploaded_file_path(config, "avatar.avatar_upload_files", data_dir)
        if uploaded is not None:
            try:
                if uploaded.stat().st_size > MAX_IMAGE_BYTES:
                    logger.warning("上传头像超过 10 MB，已跳过: %s", uploaded)
                else:
                    data = uploaded.read_bytes()
                    if data:
                        return data
            except Exception as exc:
                logger.warning("读取上传头像失败 %s: %s", uploaded, exc)
        logger.warning("头像来源为自定义上传，但没有可用的上传文件，回退 QQ 头像")

    url = bot.get("avatar_url") or ""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=5) as cli:
            async with cli.stream("GET", url) as resp:
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError("头像超过 10 MB")
                return bytes(data)
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


def build_network_chart(history: Any, tz: Any = None) -> dict[str, Any]:
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
        points = [(first_ts - (pad - i) * 60, 0.0, 0.0) for i in range(pad)] + points

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
                "text": datetime.fromtimestamp(ts, tz).strftime("%H:%M"),
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
    context: Any, bot: dict[str, Any], tz: Any = None
) -> list[dict[str, str]]:
    boot = BOOT_TIME
    boot_text = (
        boot.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        if tz
        else boot.strftime("%Y-%m-%d %H:%M:%S")
    )
    uptime = format_duration(_dt_now().timestamp() - boot.timestamp())
    plugin_count = 0
    try:
        if context is not None:
            stars = context.get_all_stars()
            plugin_count = len(
                [
                    star
                    for star in stars
                    if getattr(star, "name", "") not in {"astrbot", "builtin_commands"}
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
            "sub": f"启动于 {boot_text}",
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
    tz: Any = None,
    state: Any = None,
) -> list[dict[str, str]]:
    dpkg, pip = _package_counts(state)
    packages = f"{pip if pip is not None else '--'} pip / {dpkg if dpkg is not None else '--'} dpkg"
    disk_text = "--"
    if disks:
        d = disks[0]
        if d.used is not None and d.total is not None:
            disk_text = f"{format_bytes(d.used)} / {format_bytes(d.total)}"
    mem = memory_stat()
    cpu_brand = get_cpu_brand()
    cores = cpu_count() or cpu_count_logical() or 0
    runtime = readable_python_version()
    boot_text = (
        BOOT_TIME.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        if tz
        else BOOT_TIME.strftime("%Y-%m-%d %H:%M:%S")
    )
    protocol = " ".join(
        x for x in (bot.get("protocol_name"), bot.get("protocol_version")) if x
    ) or bot.get("adapter", "--")
    return [
        {"label": "OS", "value": _os_name()},
        {"label": "Host", "value": socket.gethostname()},
        {"label": "Kernel", "value": platform.release()},
        {"label": "Boot", "value": boot_text},
        {"label": "Runtime", "value": runtime},
        {"label": "Packages", "value": packages},
        {"label": "Shell", "value": _shell_name()},
        {"label": "Terminal", "value": "AstrBot T2I"},
        {"label": "CPU", "value": f"{cpu_brand} ({cores} Cores)"},
        {"label": "GPU", "value": _gpu_name(state)},
        {
            "label": "Memory",
            "value": f"{format_bytes(mem.used)} / {format_bytes(mem.total)}",
        },
        {"label": "Network", "value": net_interface},
        {"label": "Arch", "value": platform.machine()},
        {
            "label": "Scope",
            "value": "宿主机" if platform.system() == "Linux" else "本机",
        },
        {"label": "Disk", "value": disk_text},
        {"label": "Platform", "value": protocol},
    ]


def _resources(cpu_pct: float, mem: MemStat, swap: MemStat) -> list[dict[str, Any]]:
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


def _rule_set(config: Any) -> set[str]:
    raw = cfg_get(config, "display.redact", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(item).strip().lower() for item in raw if str(item).strip()}


def _apply_redaction(view: dict[str, Any], config: Any) -> None:
    rules = _rule_set(config)
    if not rules:
        return
    if "hostname" in rules:
        for card in view.get("overview", []):
            if card.get("label") == "主机名":
                card["value"] = "已隐藏"
        for row in view.get("system_info", []):
            if row.get("label") == "Host":
                row["value"] = "已隐藏"
    if "process" in rules:
        for proc in view.get("process", {}).get("top", []):
            proc["name"] = "已隐藏"
        view.setdefault("process", {})["redacted"] = True
    if "network" in rules:
        network = view.setdefault("network", {})
        for key in (
            "up",
            "down",
            "up_total",
            "down_total",
            "month_up",
            "month_down",
        ):
            network[key] = "--"
        network["interface"] = "已隐藏"
        network["chart"] = build_network_chart([])
    if "qq_stats" in rules:
        bot = view.setdefault("bot", {})
        for key in ("friends", "groups", "members", "recv", "sent"):
            bot[key] = None
    view["redacted"] = sorted(rules)


async def collect_all(
    context: Any = None,
    event: Any = None,
    state: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    tz = get_timezone(config)
    now = now_in_timezone(config)
    cpu_pct = await asyncio.to_thread(cpu_percent)
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
    cache_seconds = cfg_int(config, "display.website_cache_seconds", 60)
    websites = await connection_test(sites_cfg, cache_seconds=cache_seconds)

    top_n = max(1, min(30, cfg_int(config, "display.process_top_n", 8)))
    compact = cfg_bool(config, "display.compact", False)
    if compact:
        top_n = min(top_n, 5)
    proc = await asyncio.to_thread(process_info, top_n)

    def state_section(key: str) -> dict[str, Any]:
        if state is None or not hasattr(state, "get"):
            return {}
        value = state.get(key, {}) or {}
        return value if isinstance(value, dict) else {}

    net_state = state_section("net")
    disk_state = state_section("disk_io")
    recv_state = state_section("recv")

    bot = await collect_bot_info(context, event, config, recv_state)

    net_counters = psutil.net_io_counters()
    history = net_state.get("history") if isinstance(net_state, dict) else []
    chart = build_network_chart(history, tz)
    up_rate = float(net_state.get("up_rate") or 0.0)
    down_rate = float(net_state.get("down_rate") or 0.0)
    month_names = {
        1: "Jan",
        2: "Feb",
        3: "Mar",
        4: "Apr",
        5: "May",
        6: "Jun",
        7: "Jul",
        8: "Aug",
        9: "Sep",
        10: "Oct",
        11: "Nov",
        12: "Dec",
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
    overview = _overview_cards(context, bot, tz)
    system_info = (
        []
        if compact
        else await asyncio.to_thread(
            _system_info_rows, context, bot, disks, net_interface, tz, state
        )
    )

    view = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "compact": compact,
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
    _apply_redaction(view, config)
    return {"view": view}
