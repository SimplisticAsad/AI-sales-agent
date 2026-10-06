"""Polite, SSRF-safe page fetcher for company research (robots.txt respected, size/time limited)."""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from src.models.schemas import SourceDoc

USER_AGENT = "AISalesAgentResearchBot/0.1 (+company research; honours robots.txt)"
MAX_BYTES = 400_000
MAX_CHARS = 12_000
PATHS = ("", "/about", "/about-us", "/services", "/contact")


class WebFetcher(Protocol):
    def fetch_company_pages(self, website: str) -> list[SourceDoc]: ...


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\s+", " ", " ".join(p.parts))[:MAX_CHARS]


def is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


class HttpWebFetcher:
    def __init__(self, timeout: float = 10.0, client: httpx.Client | None = None):
        self._c = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False)

    def _allowed(self, base: str, url: str) -> bool:
        rp = RobotFileParser()
        try:
            r = self._c.get(urljoin(base, "/robots.txt"))
            if r.status_code >= 500:
                return False
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            return False
        return rp.can_fetch(USER_AGENT, url)

    def _get(self, url: str) -> str | None:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or not is_public_host(u.hostname):
            return None
        for _ in range(3):  # manual redirects so every hop is SSRF-checked
            try:
                r = self._c.get(url)
            except httpx.HTTPError:
                return None
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                nu = urlparse(url)
                if nu.scheme not in ("http", "https") or not nu.hostname or not is_public_host(nu.hostname):
                    return None
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", "") or len(r.content) > MAX_BYTES:
                return None
            return r.text
        return None

    def fetch_company_pages(self, website: str) -> list[SourceDoc]:
        if not website:
            return []
        base = website if website.startswith("http") else f"https://{website}"
        u = urlparse(base)
        origin = f"{u.scheme}://{u.netloc}"
        docs: list[SourceDoc] = []
        if not self._allowed(origin, origin + "/"):
            return []
        for p in PATHS:
            url = origin + p
            if not self._allowed(origin, url):
                continue
            html = self._get(url)
            if html:
                text = html_to_text(html)
                if text:
                    docs.append(SourceDoc(url=url, text=text))
        return docs


class StaticWebFetcher:
    """Test/dev fetcher returning canned documents."""

    def __init__(self, docs: dict[str, list[SourceDoc]] | list[SourceDoc]):
        self.docs = docs

    def fetch_company_pages(self, website: str) -> list[SourceDoc]:
        return self.docs if isinstance(self.docs, list) else self.docs.get(website, [])
