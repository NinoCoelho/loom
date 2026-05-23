"""Ad/tracker blocklist engine for HTTP and scraping tools.

Provides URL-level blocking of known ad, tracking, and analytics domains.
Ships with a built-in blocklist derived from EasyList and EasyPrivacy, and
supports loading external EasyList-format filter files for custom rules.

Usage::

    bl = Blocklist()
    bl.load_defaults()

    if bl.should_block("https://doubleclick.net/foo"):
        ...
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DEFAULT_BLOCKED_DOMAINS: frozenset[str] = frozenset(
    d.strip().lower().lstrip("*.")
    for d in (
        "google-analytics.com",
        "analytics.google.com",
        "googletagmanager.com",
        "googletagservices.com",
        "doubleclick.net",
        "adservice.google.com",
        "adservice.google.de",
        "pagead2.googlesyndication.com",
        "stats.g.doubleclick.net",
        "cdn.adx1.com",
        "intelligenceadx.com",
        "adsco.re",
        "mc.yandex.com",
        "mc.yandex.ru",
        "my.rtmark.net",
        "static.cloudflareinsights.com",
        "facebook.net",
        "connect.facebook.net",
        "ads.facebook.com",
        "analytics.facebook.com",
        "pixel.facebook.com",
        "ads.twitter.com",
        "analytics.twitter.com",
        "t.co",
        "amazon-adsystem.com",
        "assoc-amazon.com",
        "adnxs.com",
        "adsrvr.org",
        "casalemedia.com",
        "criteo.com",
        "criteo.net",
        "demdex.net",
        "moatads.com",
        "outbrain.com",
        "rubiconproject.com",
        "scorecardresearch.com",
        "serving-sys.com",
        "sharethis.com",
        "taboola.com",
        "tapad.com",
        "quantserve.com",
        "pubmatic.com",
        "openx.net",
        "contextweb.com",
        "bidswitch.net",
        "lijit.com",
        "bluekai.com",
        "exelator.com",
        "eyeota.net",
        "krxd.net",
        "agkn.com",
        "rlcdn.com",
        "adsymptotic.com",
        "adform.net",
        "adbrite.com",
        "zedo.com",
        "buysellads.com",
        "impact-ad.jp",
        "adingo.jp",
        "gssprt.jp",
        "ad-stir.com",
        "microad.jp",
        "smartnews-ads.com",
    )
    if d.strip()
)

_DOMAIN_RE = re.compile(r"^\|\|([a-z0-9][a-z0-9.\-]+\.[a-z]{2,})\^", re.IGNORECASE)
_EXCEPTION_RE = re.compile(r"^@@\|\|([a-z0-9][a-z0-9.\-]+\.[a-z]{2,})\^", re.IGNORECASE)


class Blocklist:
    """Domain-based URL blocklist with EasyList-format filter support.

    The blocklist maintains a set of blocked domains. A URL is blocked if
    its hostname (or any parent domain) is in the blocked set, unless the
    exact hostname is in the exception set.
    """

    def __init__(self) -> None:
        self._blocked: set[str] = set()
        self._exceptions: set[str] = set()

    @property
    def blocked_domains(self) -> set[str]:
        return set(self._blocked)

    def load_defaults(self) -> None:
        self._blocked.update(_DEFAULT_BLOCKED_DOMAINS)
        logger.debug("Loaded %d default blocked domains", len(self._blocked))

    def load_easylist_file(self, path: str | Path) -> int:
        path = Path(path)
        if not path.exists():
            logger.warning("EasyList file not found: %s", path)
            return 0
        text = path.read_text(encoding="utf-8", errors="replace")
        return self.load_easylist_text(text)

    def load_easylist_text(self, text: str) -> int:
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("!") or line.startswith("["):
                continue
            exc = _EXCEPTION_RE.match(line)
            if exc:
                domain = exc.group(1).lower()
                self._exceptions.add(domain)
                self._blocked.discard(domain)
                continue
            m = _DOMAIN_RE.match(line)
            if m:
                domain = m.group(1).lower()
                if domain not in self._exceptions:
                    self._blocked.add(domain)
                    added += 1
        return added

    def add_domain(self, domain: str) -> None:
        d = domain.strip().lower().lstrip("*.")
        if d:
            self._blocked.add(d)

    def remove_domain(self, domain: str) -> None:
        d = domain.strip().lower().lstrip("*.")
        self._blocked.discard(d)
        self._exceptions.add(d)

    def should_block(self, url: str) -> bool:
        try:
            hostname = urlparse(url).hostname
        except (ValueError, AttributeError):
            return False
        if not hostname:
            return False
        hostname = hostname.lower()
        if hostname in self._exceptions:
            return False
        parts = hostname.split(".")
        for i in range(len(parts)):
            candidate = ".".join(parts[i:])
            if candidate in self._blocked:
                return True
        return False

    def check_url(self, url: str) -> tuple[bool, str]:
        if not self.should_block(url):
            return False, ""
        try:
            hostname = urlparse(url).hostname or ""
        except (ValueError, AttributeError):
            hostname = ""
        return True, hostname


__all__ = ["Blocklist"]
