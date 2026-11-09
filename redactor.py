"""One place that decides what a rendered view is allowed to show.

The status card, the daily report and the Web dashboard each have their own
redaction switch, but they all hide the same four things: the host name,
the process list, the network figures and the QQ counters. Keeping the
rules in a single module makes it impossible for one surface to leak what
another one hides.

The rules are plain strings so they can be stored in the configuration and
compared case-insensitively.
"""

from __future__ import annotations

import re
import socket
from typing import Any

from .utils import cfg_get, redact_url

# The redaction rules the plugin understands.
RULES: tuple[str, ...] = ("hostname", "process", "network", "qq_stats")

# Placeholder shown wherever a value was removed.
HIDDEN = "已隐藏"
HIDDEN_NUMBER = "--"

# Address-shaped tokens that must not reach a chat or the dashboard when the
# hostname rule is on. Plain words and codes ("HTTP 500", "ReadTimeout") stay
# readable: hiding every non-empty error text made the monitor cards useless.
#
# The URL stop set also excludes CJK punctuation: a URL directly followed by
# "，如果超时请重试" used to swallow the readable tail ("。" "，" "：" ... are
# not whitespace, so the character class ran past the URL into the sentence,
# 2026-11-08 reverse audit). Half-width "]" stays OUT on purpose: an IPv6
# literal host ("http://[fe80::1]/path") must survive the URL pass whole.
_URL_STOP = r"[^\s'\"）)，。：；！？、》]"
_ADDRESS_RE = re.compile(
    # host:port must come first: with the IPv4 branch ahead of it,
    # "10.1.2.3:8080" matched only the address and left the port dangling
    # in the masked text ("（已隐藏地址）:8080", v2.20.20).
    r"(?:\b[A-Za-z][A-Za-z0-9_.-]*|\b(?:\d{1,3}\.){3}\d{1,3}):\d{2,5}\b"  # host:port
    r"|\b(?:\d{1,3}\.){3}\d{1,3}\b"  # IPv4
    r"|https?://" + _URL_STOP + r"+"  # URL
    r"|\b(?=[0-9a-f:]*[a-f])[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){2,}\b",  # IPv6
    re.IGNORECASE,
)

# The IPv6 branch above only matched the full eight-group form. The compact
# notation ("2001:db8::1", "fe80::1%eth0", "::1") is what people actually
# write, and it slipped through the hostname rule: the dashboard and the
# daily report showed the complete internal address (v2.20.14).
#
# The candidate pattern is deliberately loose -- it also catches "12:34:56"
# and "a:b" -- and ``_is_ipv6_literal`` decides what is really an address.
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z:.])"
    r"[0-9A-Fa-f]*:[0-9A-Fa-f:]*(?::[0-9A-Fa-f:]*)*[0-9A-Fa-f]*"
    r"(?:%[0-9A-Za-z._~-]+)?"
    r"(?![0-9A-Za-z:])"
)

# The URL branch of _ADDRESS_RE as a standalone pattern: used as the FIRST
# pass in ``mask_address_tokens`` so a URL is hidden whole instead of being
# truncated at the hostname pass's placeholder (2026-11-08 audit). The stop
# set excludes CJK punctuation so the sentence after the URL survives.
_URL_RE = re.compile(r"https?://" + _URL_STOP + r"+", re.IGNORECASE)


def _is_ipv6_literal(token: str) -> bool:
    """True when ``token`` really is an IPv6 literal.

    Rules:
      * at least two colons;
      * the compact form ("::") may appear at most once and leaves room for
        at most seven non-empty groups;
      * the full form needs exactly eight groups of one to four hex digits.
    """

    body = str(token or "").split("%", 1)[0]
    if body.count(":") < 2:
        return False
    if not re.fullmatch(r"[0-9A-Fa-f:]+", body):
        return False
    if "::" in body:
        if body.count("::") > 1:
            return False
        groups = [part for part in body.replace("::", ":").split(":") if part]
        if not groups or len(groups) > 7:
            return False
        return all(1 <= len(part) <= 4 for part in groups)
    groups = body.split(":")
    if len(groups) != 8:
        return False
    return all(1 <= len(part) <= 4 for part in groups)


def _address_replacement(match: re.Match[str]) -> str:
    """Hide an address-shaped token unless it is a false positive.

    The loose IPv6 branch also swallows MAC addresses ("a4:b1:c2:d3:e4:f5")
    and similar colon-separated hex groups (RAID serials, ARP table rows),
    which are not addresses -- hiding them replaced the whole diagnostic
    with a placeholder. The exemption is limited to tokens that look like
    IPv6 candidates (ONLY hex digits and colons, and at least TWO colons):
    a bare "db:5432" host:port matches the hex shape too and used to slip
    through the exemption, leaking the host the hostname rule must hide
    (v2.20.25 reverse audit). A timestamp ("07:01:30") does not contain the
    a-f letters the branch requires and is not matched in the first place.
    """

    token = match.group(0)
    if (
        token.count(":") >= 2
        and re.fullmatch(r"[0-9A-Fa-f:]+", token)
        and not _is_ipv6_literal(token)
    ):
        return token
    return "（已隐藏地址）"


def _has_real_address(text: str) -> bool:
    """True when ``text`` contains a token the replacer would hide."""

    return any(
        _address_replacement(match) != match.group(0)
        for match in _ADDRESS_RE.finditer(text)
    )


def _mask_ipv6(text: str) -> str:
    """Replace every IPv6 literal in ``text`` with a placeholder."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return "（已隐藏地址）" if _is_ipv6_literal(token) else token

    return _IPV6_CANDIDATE_RE.sub(_replace, text)


# A bare host name with at least one dot ("nas.local", "internal.example").
# The hostname rule used to match only address-shaped tokens, so a machine
# name like "PC-20260627WVRG" or a dotted internal name passed through in
# monitor names, error texts and event names (v2.20.5).
#
# The boundaries are explicit ASCII lookarounds instead of ``\b``: with
# Unicode words, a Chinese character directly attached to the host
# ("主机nas.local.example.com超时") creates no word boundary, so the match
# started mid-host and leaked the first label and the TLD (v2.20.24 audit).
_HOSTNAME_RE = re.compile(
    r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61})\.)+[A-Za-z]{2,63}"
    r"(?![A-Za-z0-9-])"
)

# ``_HOSTNAME_RE`` also matches file names and config keys ("README.md",
# "alerts.enabled", "module.py"). In a free-form diagnostic text those are
# common and must stay readable, so only tokens whose last label is a known
# host suffix are treated as a host name.
_HOST_SUFFIXES = {
    "ai",
    "app",
    "arpa",
    "biz",
    "cc",
    "club",
    "cloud",
    "cn",
    "co",
    "com",
    "company",
    "corp",
    "de",
    "dev",
    "digital",
    "eu",
    "example",
    "fr",
    "group",
    "hk",
    "home",
    "info",
    "internal",
    "io",
    "jp",
    "kr",
    "lan",
    "life",
    "link",
    "live",
    "local",
    "localhost",
    "me",
    "media",
    "net",
    "network",
    "online",
    "org",
    "pro",
    "ru",
    "sg",
    "shop",
    "site",
    "solutions",
    "space",
    "store",
    "support",
    "systems",
    "tech",
    "test",
    "tools",
    "top",
    "tv",
    "tw",
    "uk",
    "us",
    "wiki",
    "work",
    "world",
    "xyz",
}


def _mask_bare_hostname(match: re.Match[str]) -> str:
    token = match.group(0)
    suffix = token.rsplit(".", 1)[-1].lower()
    if suffix not in _HOST_SUFFIXES:
        return token
    # A single slash before the token marks a file/dir name
    # ("/srv/backup.store", "/Applications/Safari.app"), not a host. A UNC
    # path ("//nas.local/share") has two slashes, so the host is still
    # masked. Without this, adding common TLDs hid ordinary paths and made
    # the error text unreadable (2026-11-06 reverse audit).
    start = match.start()
    if (
        start > 0
        and match.string[start - 1] == "/"
        and (start < 2 or match.string[start - 2] != "/")
    ):
        return token
    return "（已隐藏地址）"


def normalize_rules(raw: Any) -> set[str]:
    """Turn a configuration value into a set of rule names.

    Accepts a list, a single string or ``None`` so callers can hand over
    whatever the configuration happens to hold.
    """

    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {
        str(item).strip().lower()
        for item in raw
        if str(item).strip() and str(item).strip().lower() in RULES
    }


def read_rules(config: Any, key: str = "display.redact") -> set[str]:
    """Read the rules of one surface from the plugin configuration.

    ``key`` is a dotted path such as ``display.redact`` or
    ``display.page_redact``. Every surface owns its switch outright: a
    missing or empty value means "hide nothing here", never "use another
    surface's rules".
    """

    return normalize_rules(cfg_get(config, key))


class Redactor:
    """Apply the redaction rules of one surface to the data it exposes."""

    def __init__(self, rules: Any = None):
        self.rules = normalize_rules(rules)
        try:
            self._own_hostname = socket.gethostname() or ""
        except Exception:
            self._own_hostname = ""

    def _contains_host_name(self, value: str) -> bool:
        """True when the text carries an address or a host name."""

        # The same MAC/timestamp guard the token replacer uses: a raw
        # ``_ADDRESS_RE.search`` treated "a4:b1:c2:d3:e4:f5" as an address
        # and replaced the whole error text with a placeholder (v2.20.24
        # audit).
        if _has_real_address(value):
            return True
        # The compact IPv6 notation is not covered by _ADDRESS_RE (v2.20.14).
        if any(
            _is_ipv6_literal(match.group(0))
            for match in _IPV6_CANDIDATE_RE.finditer(value)
        ):
            return True
        own = self._own_hostname
        if own and len(own) >= 3:
            # Word-ish boundaries, not a raw substring: a 3-5 character
            # hostname ("web", "app", "db1") used to match "Website",
            # "Application" and "db10", hiding entire unrelated names and
            # corrupting readable error text ("已隐藏lication"). Hyphens and
            # dots remain separators so "web-01"/"app.example.com" still
            # match; only an adjacent letter/digit suppresses it.
            pattern = r"(?<![A-Za-z0-9])" + re.escape(own) + r"(?![A-Za-z0-9])"
            if re.search(pattern, value, flags=re.IGNORECASE):
                return True
        # A bare dotted host name with a known suffix counts; a file name or
        # config key ("README.md", "alerts.enabled") must not hide the whole
        # text or name.
        return any(
            _mask_bare_hostname(match) != match.group(0)
            for match in _HOSTNAME_RE.finditer(value)
        )

    def __bool__(self) -> bool:
        return bool(self.rules)

    def __contains__(self, rule: str) -> bool:
        return str(rule).strip().lower() in self.rules

    @property
    def hostname(self) -> bool:
        return "hostname" in self.rules

    @property
    def process(self) -> bool:
        return "process" in self.rules

    @property
    def network(self) -> bool:
        return "network" in self.rules

    @property
    def qq_stats(self) -> bool:
        return "qq_stats" in self.rules

    @property
    def sorted_rules(self) -> list[str]:
        return sorted(self.rules)

    # ------------------------------------------------------------------
    # status card
    # ------------------------------------------------------------------

    def apply_card(self, view: dict[str, Any]) -> dict[str, Any]:
        """Redact a collected status-card view in place."""

        if not self.rules:
            return view
        if self.hostname:
            for card in view.get("overview", []):
                if card.get("label") == "主机名":
                    card["value"] = HIDDEN
            for row in view.get("system_info", []):
                if row.get("label") == "Host":
                    row["value"] = HIDDEN
            # The website table repeats the probe target and its error text;
            # an internal address there leaked even with the hostname rule on.
            for site in view.get("websites", []) or []:
                if not isinstance(site, dict):
                    continue
                name = redact_url(str(site.get("name") or ""))
                masked = self.mask_addresses(name)
                site["name"] = HIDDEN if masked != name else name
                if site.get("error"):
                    site["error"] = self.mask_addresses(site["error"])
            # A mount point can itself carry a host ("//nas.local/share",
            # "/mnt/192.168.1.5/data"). The alert rules already mask it; the
            # status card and dashboard used to print the raw path (v2.20.22).
            for disk in view.get("disks", []) or []:
                if isinstance(disk, dict) and disk.get("path"):
                    disk["path"] = self.mask_address_tokens(disk["path"])
        if self.process:
            for proc in view.get("process", {}).get("top", []):
                proc["name"] = HIDDEN
            view.setdefault("process", {})["redacted"] = True
        if self.network:
            network = view.setdefault("network", {})
            for key in (
                "up",
                "down",
                "up_total",
                "down_total",
                "month_up",
                "month_down",
            ):
                network[key] = HIDDEN_NUMBER
            network["interface"] = HIDDEN
            network["chart"] = _empty_chart()
            # The sysinfo table repeats the interface name; hiding only the
            # network card left eth0/ens3 visible there.
            for row in view.get("system_info", []):
                if isinstance(row, dict) and row.get("label") == "Network":
                    row["value"] = HIDDEN
        if self.qq_stats:
            bot = view.setdefault("bot", {})
            for key in ("friends", "groups", "members", "recv", "sent"):
                bot[key] = None
        view["redacted"] = self.sorted_rules
        return view

    # ------------------------------------------------------------------
    # daily report
    # ------------------------------------------------------------------

    def apply_report(self, section: dict[str, Any], name: str) -> dict[str, Any]:
        """Redact one section of a daily-report view in place.

        The report renders its own sections, so the report knows which
        section it is and can be redacted precisely.
        """

        if not self.rules:
            return section
        if name == "qq" and self.qq_stats:
            section["available"] = False
            section["empty_reason"] = "已按脱敏配置隐藏 QQ 统计"
            for key in ("days", "labels"):
                section[key] = []
            for key in ("yesterday", "previous"):
                section[key] = {}
            section["reset_days"] = []
            section["sent_available"] = False
            section["snapshot_note"] = None
            section["recv_note"] = None
            section["charts"] = {}
            section["note"] = ""
        if name == "process" and self.process:
            for item in section.get("top", []) or []:
                if isinstance(item, dict):
                    item["name"] = HIDDEN
        return section

    # ------------------------------------------------------------------
    # web dashboard
    # ------------------------------------------------------------------

    def dashboard_host(self, hostname: str) -> str:
        """Return the host name the dashboard may show."""

        return HIDDEN if self.hostname else hostname

    def mask_addresses(self, text: Any) -> str:
        """Hide probe errors that embed a host, address or URL.

        Only applied when the ``hostname`` rule is on, and only when the text
        actually contains an address-shaped token. Replacing every non-empty
        error with a placeholder also hid "HTTP 500 (expected 200)" and
        "probe timed out", which made the monitor cards useless for
        diagnosis while adding no privacy.
        """

        value = str(text or "")
        if not value or not self.hostname:
            return value
        if not self._contains_host_name(value):
            return value
        return "已隐藏（错误详情含主机或地址信息）"

    def mask_address_tokens(self, text: Any) -> str:
        """Replace address-shaped tokens inside a longer text.

        ``mask_addresses`` replaces the *whole* text; a forwarded third-party
        message ("10.0.0.8 无法连接") should keep its readable part and only
        lose the address (v2.20.8).
        """

        value = str(text or "")
        if not value or not self.hostname:
            return value
        # URLs before anything else: the hostname pass would mask only the
        # host inside a URL, and its placeholder "（已隐藏地址）" ends with
        # "）" -- which is in the URL branch's stop set. The URL branch then
        # truncated right after the placeholder and the path plus query
        # string leaked ("https://host/api?token=abc 超时" became
        # "（已隐藏地址））/api?token=abc 超时", 2026-11-08 audit). A whole
        # URL is one identifying blob: hide it completely.
        masked = _URL_RE.sub("（已隐藏地址）", value)
        # Host names next: the pattern consumes "1.2.3.4.example.com" as one
        # token, so the IP pass below cannot leave an isolated "." behind.
        masked = _HOSTNAME_RE.sub(_mask_bare_hostname, masked)
        masked = _ADDRESS_RE.sub(_address_replacement, masked)
        # The compact IPv6 notation needs its own pass: the regex above only
        # knows the full eight-group form (v2.20.14).
        masked = _mask_ipv6(masked)
        own = self._own_hostname
        if own and len(own) >= 3:
            pattern = r"(?<![A-Za-z0-9])" + re.escape(own) + r"(?![A-Za-z0-9])"
            masked = re.sub(pattern, HIDDEN, masked, flags=re.IGNORECASE)
        # A host name that carried a port leaves the port dangling after the
        # host is masked ("（已隐藏地址）:8080"). Drop a port directly attached
        # to either placeholder; a bare port elsewhere is not identifying.
        masked = re.sub(
            rf"({re.escape(HIDDEN)}|（已隐藏地址）):\d{{2,5}}\b", r"\1", masked
        )
        # A URL whose path ends in a closing bracket ("https://host/(a)") is
        # cut at the ")" by the URL pass when the next character is not part
        # of the URL ("...);token=SECRET"). The query string then survives the
        # placeholder. Only this exact shape -- a masked URL immediately
        # followed by ")" and a leftover credential query -- is swept up here;
        # plain text containing "token=" and the readable tail after a normal
        # URL are never touched (2026-11-09 audit).
        masked = re.sub(
            r"(（已隐藏地址）\))[?&;]?[^\s）)]*?"
            # The lookbehind must allow a "_" prefix: access_token /
            # refresh_token / client_secret were left visible, while a
            # letter prefix (monkey=, tokenless=) is still rejected.
            r"(?<![A-Za-z0-9])(?:token|password|passwd|secret|credential|api[_-]?key)"
            r"(?![A-Za-z0-9_])[^\s）)]*",
            r"\1",
            masked,
            flags=re.IGNORECASE,
        )
        return masked

    def mask_name(self, name: Any) -> str:
        """Hide a monitor/website *name* that is itself an address or URL.

        The dashboard/report surfaces strip query tokens via ``redact_url``
        but kept the host+port, so with the ``hostname`` rule on a target
        named ``https://internal:8443/health`` still leaked the internal host
        on the dashboard and in forwarded reports. When the name carries an
        address-shaped token, replace the whole name -- mirroring the status
        card's ``apply_card`` behaviour -- instead of leaving the host visible.
        """

        value = str(name or "")
        if not value or not self.hostname:
            return value
        if not self._contains_host_name(value):
            return value
        return "已隐藏"


def _empty_chart() -> dict[str, Any]:
    """An empty network chart with the same shape as a filled one.

    The values mirror the empty chart the status-card collector produces,
    so the template can render both without special cases.
    """

    width = 560
    height = 150
    left = 54
    right = 10
    top = 12
    bottom = 24
    return {
        "width": width,
        "height": height,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "plot_w": width - left - right,
        "plot_h": height - top - bottom,
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
