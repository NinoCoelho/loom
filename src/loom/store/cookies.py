from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class CookieStore(Protocol):
    async def get_cookies(self, domain: str) -> dict[str, str] | None: ...

    async def save_cookies(self, domain: str, cookies: dict[str, str]) -> None: ...

    async def list_domains(self) -> list[str]: ...


def _encode_domain(domain: str) -> str:
    return domain.replace(":", "_")


def _parse_netscape_cookies(text: str) -> dict[str, str]:
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
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append(f".{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    return "\n".join(lines) + "\n"


class FilesystemCookieStore:
    def __init__(self, cookie_dir: Path) -> None:
        self._dir = cookie_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, domain: str) -> Path:
        return self._dir / f"{_encode_domain(domain)}.cookies.txt"

    async def get_cookies(self, domain: str) -> dict[str, str] | None:
        path = self._path(domain)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        cookies = _parse_netscape_cookies(text)
        return cookies if cookies else None

    async def save_cookies(self, domain: str, cookies: dict[str, str]) -> None:
        path = self._path(domain)
        content = _dict_to_netscape(cookies, domain)
        path.write_text(content, encoding="utf-8")

    async def list_domains(self) -> list[str]:
        domains: list[str] = []
        for p in sorted(self._dir.glob("*.cookies.txt")):
            name = p.name[: -len(".cookies.txt")]
            domains.append(name)
        return domains
