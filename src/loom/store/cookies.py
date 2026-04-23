"""Cookie persistence for web scraping.

:class:`CookieStore` is a pluggable protocol; the default
:class:`FilesystemCookieStore` persists cookies per domain in Netscape
cookies.txt format. Used by
:class:`~loom.scrape.scrapling.ScraplingProvider` for cookie-based auth
retry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class CookieStore(Protocol):
    """Protocol for a domain-keyed cookie store."""

    async def get_cookies(self, domain: str) -> dict[str, str] | None:
        """Retrieve cookies for *domain*, or ``None`` if none exist."""
        ...

    async def save_cookies(self, domain: str, cookies: dict[str, str]) -> None:
        """Persist *cookies* for *domain*."""
        ...

    async def list_domains(self) -> list[str]:
        """Return all domains with stored cookies."""
        ...


def _encode_domain(domain: str) -> str:
    """Make a domain filesystem-safe (replace colons with underscores)."""
    return domain.replace(":", "_")


def _parse_netscape_cookies(text: str) -> dict[str, str]:
    """Parse Netscape cookies.txt format into a name → value dict."""
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


def _dict_to_netscape(cookies: dict[str, str], domain: str) -> str:
    """Serialise a cookie dict into Netscape cookies.txt format."""
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append(f".{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    return "\n".join(lines) + "\n"


class FilesystemCookieStore:
    """Default :class:`CookieStore` backed by per-domain files on disk.

    Cookies are stored in Netscape cookies.txt format under a configurable
    directory.
    """

    def __init__(self, cookie_dir: Path) -> None:
        """Create the store rooted at *cookie_dir*."""
        self._dir = cookie_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, domain: str) -> Path:
        """Filesystem path for a domain's cookie file."""
        return self._dir / f"{_encode_domain(domain)}.cookies.txt"

    async def get_cookies(self, domain: str) -> dict[str, str] | None:
        """Read and parse cookies for *domain*."""
        path = self._path(domain)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        cookies = _parse_netscape_cookies(text)
        return cookies if cookies else None

    async def save_cookies(self, domain: str, cookies: dict[str, str]) -> None:
        """Write *cookies* for *domain*."""
        path = self._path(domain)
        content = _dict_to_netscape(cookies, domain)
        path.write_text(content, encoding="utf-8")

    async def list_domains(self) -> list[str]:
        """Enumerate domains with stored cookie files."""
        domains: list[str] = []
        for p in sorted(self._dir.glob("*.cookies.txt")):
            name = p.name[: -len(".cookies.txt")]
            domains.append(name)
        return domains
