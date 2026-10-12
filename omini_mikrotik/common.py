"""Parsing helpers shared by the collection and the changes.

RouterOS returns every value as a string ("true", "1500", "1Gbps", "1w2d3h"),
so everything is parsed here; what cannot be parsed stays None.
"""

from __future__ import annotations

import re
from typing import Any

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("-", ":")
    return v if MAC.match(v) and v != "00:00:00:00:00:00" else None


def number(value: Any) -> float | None:
    """'35' → 35.0, '-5.792dBm' → -5.792, '3.237V' → 3.237, '' → None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+(\.\d+)?", value)
        if m:
            return float(m.group())
    return None


def counter(value: Any) -> int | None:
    n = number(value)
    return int(n) if n is not None and n >= 0 else None


def flag(value: Any) -> bool | None:
    """RouterOS booleans: 'true'/'false' (REST), 'yes'/'no' (some monitors)."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, str) or not value:
        return None
    v = value.strip().lower()
    if v in ("true", "yes"):
        return True
    if v in ("false", "no"):
        return False
    return None


RATE_UNITS = {"": 1e-6, "k": 1e-3, "m": 1, "g": 1e3, "t": 1e6}


def rate_mbps(value: Any) -> float | None:
    """'1Gbps' → 1000, '2.5Gbps' → 2500, '866.7Mbps-80MHz/2S/SGI' → 866.7, '54Mbps' → 54."""
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)bps", value, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1)) * RATE_UNITS[m.group(2).lower()]


def speed_setting(value: Any) -> int | None:
    """Ethernet speed setting: '1G-baseT-full' → 1000, '100M-baseT-half' → 100,
    '10G-baseSR-LR' → 10000."""
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)([MG])-", value)
    if not m:
        return None
    v = float(m.group(1))
    return int(v * 1000) if m.group(2) == "G" else int(v)


DURATION = re.compile(r"(\d+)([wdhms])")
SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def duration_s(value: Any) -> int | None:
    """'1w2d3h4m5s' → seconds; also '2d03:04:05' and '00:09:57'."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    total = 0
    m = re.search(r"(\d+):(\d{2}):(\d{2})$", v)
    if m:
        h, mi, s = (int(x) for x in m.groups())
        total = h * 3600 + mi * 60 + s
        v = v[: m.start()]
    parts = DURATION.findall(v)
    if not parts and not m:
        return None
    total += sum(int(n) * SECONDS[u] for n, u in parts)
    return total


def as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def as_object(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def enabled(row: dict[str, Any]) -> bool:
    return not flag(row.get("disabled")) and not flag(row.get("invalid"))


def first_name(value: Any) -> str | None:
    """'ether2,bridge' → 'ether2' (neighbors list the port, then its bridge)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.split(",")[0].strip() or None


def names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not isinstance(value, str):
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def vlan_ids(value: Any) -> list[int]:
    """'10,20,30-32' → [10, 20, 30, 31, 32]."""
    out: list[int] = []
    for part in names(value):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            continue
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        out.extend(v for v in range(lo, min(hi, 4094) + 1) if 1 <= v <= 4094)
    return out
