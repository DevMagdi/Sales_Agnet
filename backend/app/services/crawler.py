"""Polite, SSRF-safe crawler.

Rules: identify as MantiqOutreachBot/1.0 (+/bot), obey robots.txt and crawl-delay, at most
CRAWL_MAX_PAGES pages per site (home, about, products/services, contact, careers/suppliers),
10s timeout, 2MB cap, per-host concurrency 1, only public IPs (private/link-local blocked,
re-resolved after every redirect), only http/https.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.core.config import Settings
from app.core.errors import ValidationFailed
from app.core.logging import get_logger
from app.core.text import normalise_space

logger = get_logger("crawler")

CANDIDATE_PATHS = [
    ("", "home"),
    ("/about", "about"),
    ("/about-us", "about"),
    ("/products", "products"),
    ("/services", "services"),
    ("/contact", "contact"),
    ("/contact-us", "contact"),
    ("/careers", "careers"),
    ("/suppliers", "suppliers"),
    ("/vendor-registration", "suppliers"),
    ("/supplier-registration", "suppliers"),
]
PRIORITY = ["home", "about", "products", "services", "contact", "suppliers", "careers"]


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    text: str
    title: str
    kind: str
    links: list[str] = field(default_factory=list)
    mailtos: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CrawlResult:
    pages: list[FetchedPage]
    skipped: list[dict[str, str]]
    robots_denied: bool = False

    @property
    def combined_text(self) -> str:
        return "\n\n".join(f"--- PAGE: {p.url} ---\n{p.text}" for p in self.pages)


class SSRFSafeResolver:
    """Blocks private, loopback, link-local, multicast and reserved addresses."""

    BLOCKED = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
    )

    def __init__(self, allow_local: bool = False) -> None:
        self.allow_local = allow_local

    def is_public_host(self, host: str) -> bool:
        if not host:
            return False
        if self.allow_local and host in ("localhost", "127.0.0.1", "::1"):
            return True
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                return False
            if self.allow_local and (ip.is_loopback or ip.is_private):
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
            if any(ip in network for network in self.BLOCKED if network.version == ip.version):
                return False
        return True

    def validate_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValidationFailed(f"blocked scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise ValidationFailed("blocked url: no host")
        if not self.is_public_host(parsed.hostname):
            raise ValidationFailed(f"blocked host (not public): {parsed.hostname}")
        return url


class PoliteCrawler:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: SSRFSafeResolver | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._resolver = resolver or SSRFSafeResolver(settings.crawl_allow_local_hosts)
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.crawl_timeout_seconds,
                follow_redirects=True,
                max_redirects=3,
                headers={"User-Agent": f"{self.settings.crawler_user_agent} (+{self.settings.bot_url})"},
            )
        return self._client

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Lock()
        return self._host_locks[host]

    async def robots_allows(self, url: str) -> tuple[bool, float]:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host in self._robots:
            parser = self._robots[host]
            return parser.can_fetch("*", url), float(getattr(parser, "crawl_delay", 0) or 0)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.parse([])
        try:
            self._resolver.validate_url(robots_url)
            response = await self._http().get(robots_url, timeout=5.0)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            elif response.status_code in (401, 403):
                setattr(parser, "disallow_all", True)  # noqa: B010 - set by urllib, missing from stubs
        except (httpx.HTTPError, ValidationFailed) as exc:
            logger.info("robots_unavailable", host=host, error=str(exc)[:120])
        self._robots[host] = parser
        allowed = parser.can_fetch("*", url)
        delay = 0.0
        try:
            raw_delay = parser.crawl_delay("*")
            delay = float(raw_delay) if raw_delay else 0.0
        except (AttributeError, ValueError):
            delay = 0.0
        return allowed, delay

    async def fetch(self, url: str) -> FetchedPage | None:
        try:
            self._resolver.validate_url(url)
        except ValidationFailed as exc:
            logger.warning("crawl_blocked", url=url, reason=str(exc))
            return None
        allowed, delay = await self.robots_allows(url)
        if not allowed:
            logger.info("robots_denied", url=url)
            return None
        host = urlparse(url).hostname or ""
        wait = max(self.settings.crawl_delay_seconds, delay)
        async with self._lock(host):
            await asyncio.sleep(wait)
            try:
                async with self._http().stream("GET", url) as response:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.crawl_max_page_bytes:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    content_type = response.headers.get("content-type", "")
                    if "html" not in content_type and "text" not in content_type:
                        return None
                    html_text = raw.decode(response.encoding or "utf-8", errors="ignore")
            except (httpx.HTTPError, UnicodeDecodeError) as exc:
                logger.info("fetch_failed", url=url, error=str(exc)[:150])
                return None
        if response.history:
            try:
                self._resolver.validate_url(str(response.url))
            except ValidationFailed:
                return None
        tree = HTMLParser(html_text)
        title_node = tree.css_first("title")
        title = normalise_space(title_node.text()) if title_node is not None else ""
        for node in tree.css("script, style, noscript, svg"):
            node.decompose()
        text = normalise_space(tree.body.text(separator=" ") if tree.body else tree.text(separator=" "))
        links = [
            urljoin(str(response.url), a.attributes.get("href", ""))
            for a in tree.css("a[href]")
            if a.attributes.get("href")
        ]
        mailtos = [
            (a.attributes.get("href") or "").replace("mailto:", "").split("?")[0].strip().lower()
            for a in tree.css('a[href^="mailto:"]')
            if a.attributes.get("href")
        ]
        page_text = text
        try:
            import trafilatura

            extracted = trafilatura.extract(
                html_text, include_comments=False, include_tables=True, favor_precision=True
            )
            if extracted and len(extracted) > len(page_text) * 0.4:
                page_text = normalise_space(extracted)
        except Exception:  # noqa: BLE001 - trafilatura is an enhancement, not a dependency
            pass
        return FetchedPage(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            text=page_text[:120_000],
            title=title,
            kind="",
            links=links[:200],
            mailtos=sorted(set(mailtos)),
        )

    @staticmethod
    def candidate_urls(website: str) -> list[str]:
        parsed = urlparse(website if "://" in website else f"https://{website}")
        base = f"{parsed.scheme}://{parsed.netloc}"
        urls: list[tuple[str, str]] = []
        for path, kind in CANDIDATE_PATHS:
            urls.append((f"{base}{path}", kind))
        return [url for url, _kind in urls]

    async def crawl_site(self, website: str, *, max_pages: int | None = None) -> CrawlResult:
        limit = max_pages or self.settings.crawl_max_pages
        pages: list[FetchedPage] = []
        skipped: list[dict[str, str]] = []
        seen: set[str] = set()
        root = website if "://" in website else f"https://{website}"
        home = await self.fetch(root)
        if home is None:
            return CrawlResult(pages=[], skipped=[{"url": root, "reason": "home_unreachable"}], robots_denied=True)
        home.kind = "home"
        pages.append(home)
        seen.add(home.final_url.rstrip("/"))
        seen.add(root.rstrip("/"))
        discovered: list[tuple[str, str]] = []
        for link in home.links:
            parsed = urlparse(link)
            if parsed.netloc and parsed.netloc != urlparse(home.final_url).netloc:
                continue
            lower = link.lower()
            for keyword, kind in (
                ("about", "about"),
                ("product", "products"),
                ("service", "services"),
                ("contact", "contact"),
                ("supplier", "suppliers"),
                ("vendor", "suppliers"),
                ("career", "careers"),
                ("job", "careers"),
            ):
                if keyword in lower:
                    discovered.append((link.split("#")[0], kind))
                    break
        discovered.sort(key=lambda item: PRIORITY.index(item[1]) if item[1] in PRIORITY else 99)
        queue = discovered + [(u, k) for u, k in zip(self.candidate_urls(website), [c[1] for c in CANDIDATE_PATHS], strict=False)]
        for url, kind in queue:
            if len(pages) >= limit:
                break
            normalised = url.rstrip("/")
            if normalised in seen:
                continue
            seen.add(normalised)
            page = await self.fetch(url)
            if page is None:
                skipped.append({"url": url, "reason": "not_fetched_or_disallowed"})
                continue
            page.kind = kind
            if any(p.final_url == page.final_url for p in pages):
                continue
            pages.append(page)
        return CrawlResult(pages=pages, skipped=skipped)

    async def is_stale(self, last_crawled_at: datetime | None, days: int | None = None) -> bool:
        if last_crawled_at is None:
            return True
        if last_crawled_at.tzinfo is None:
            last_crawled_at = last_crawled_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - last_crawled_at > timedelta(days=days or self.settings.crawl_recrawl_after_days)


def extract_contact_signals(pages: list[FetchedPage]) -> dict[str, Any]:
    """Published emails/phones, supplier-registration page, ERP mention, languages."""
    emails: list[dict[str, str]] = []
    seen: set[str] = set()
    for page in pages:
        for email in page.mailtos:
            if email and email not in seen:
                seen.add(email)
                emails.append({"email": email, "source_url": page.url})
    text = "\n".join(p.text for p in pages)
    lower = text.lower()
    supplier_page = any(p.kind == "suppliers" for p in pages) or any(
        marker in lower for marker in ("vendor registration", "supplier registration", "تسجيل الموردين", "التسجيل كمورد")
    )
    erp_mention = any(
        marker in lower
        for marker in ("sap", "oracle", "erp", "dynamics", "odoo", "e-procurement", "نظام المشتريات")
    )
    return {
        "emails": emails,
        "supplier_registration_page": supplier_page,
        "erp_mention": erp_mention,
        "has_arabic": any("\u0600" <= ch <= "\u06FF" for ch in text[:20000]),
        "text_length": len(text),
    }
