"""Where the cache, config and login live, per platform.

Cache (safe to delete; rebuilt by syncing):
    OPENRTX_CACHE, else ~/Library/Caches/openrtx (macOS),
    $XDG_CACHE_HOME/openrtx or ~/.cache/openrtx (Linux),
    %LOCALAPPDATA%\\openrtx\\cache (Windows)
Config and login:
    OPENRTX_CONFIG, else $XDG_CONFIG_HOME/openrtx or ~/.config/openrtx
    (macOS and Linux), %APPDATA%\\openrtx (Windows)

The config file is `config` in the config dir, `key = value` per line, e.g.
    git.repo = ~/workspace/OpenRTX
    github.org = OpenRTX
    period.github = 30m
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys


def cache_dir():
    env = os.environ.get("OPENRTX_CACHE")
    if env:
        return os.path.expanduser(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "openrtx", "cache")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/openrtx")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "openrtx")


def config_dir():
    env = os.environ.get("OPENRTX_CONFIG")
    if env:
        return os.path.expanduser(env)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return os.path.join(base, "openrtx")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "openrtx")


def ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def db_path(source):
    """The SQLite file holding one source's cache."""
    return os.path.join(ensure(cache_dir()), "%s.sqlite" % source)


def load_config():
    """`key = value` lines from the config file; missing file means no settings."""
    conf = {}
    try:
        with open(os.path.join(config_dir(), "config")) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    conf[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return conf


def parse_age(text):
    """90s, 15m, 2h, 1d (or plain seconds) -> timedelta."""
    m = re.fullmatch(r"(\d+)([smhd]?)", str(text).strip())
    if not m:
        raise ValueError("expected an age such as 90s, 15m, 2h or 1d, not %r" % text)
    unit = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return dt.timedelta(seconds=int(m.group(1)) * unit)


def format_age(delta):
    secs = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s" % (secs // size, unit)
    return "%ds" % secs
