import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    body: bytes
    fetched_at: datetime
    encoding: str | None = None
    status_code: int = 200
    content_type: str = "text/html"


@dataclass(frozen=True)
class FetchedResource:
    url: str
    body: bytes
    fetched_at: datetime
    content_type: str
    encoding: str | None = None
    status_code: int = 200


class ArticleFetcher(Protocol):
    def fetch(self, url: str) -> FetchedArticle: ...


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_reserved or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return address.ipv4_mapped is None and address.sixtofour is None and address.teredo is None
    return True


class PublicWebFetcher:
    def __init__(
        self,
        *,
        timeout: float = 10,
        max_bytes: int = 2_000_000,
        max_redirects: int = 3,
        user_agent: str = "Prawdowid/0.1 (article extraction)",
    ) -> None:
        if timeout <= 0 or max_bytes <= 0 or max_redirects < 0 or not user_agent.strip():
            raise ValueError("Invalid fetch limits")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent

    def fetch(self, url: str, *, allowed_hosts: set[str] | None = None) -> FetchedArticle:
        resource = self.fetch_resource(
            url,
            allowed_hosts=allowed_hosts,
            # Official guides and reports are often PDFs (e.g. the gov.pl
            # safety guide); the caller extracts text by content_type.
            accepted_content_types={"text/html", "application/xhtml+xml", "application/pdf"},
            accept_header="text/html, application/xhtml+xml, application/pdf;q=0.9",
            content_type_description="HTML article or PDF",
        )
        return FetchedArticle(
            url=resource.url,
            body=resource.body,
            fetched_at=resource.fetched_at,
            encoding=resource.encoding,
            status_code=resource.status_code,
            content_type=resource.content_type,
        )

    def fetch_resource(
        self,
        url: str,
        *,
        allowed_hosts: set[str] | None,
        accepted_content_types: set[str],
        accept_header: str = "*/*",
        content_type_description: str = "resource",
    ) -> FetchedResource:
        if (
            not accepted_content_types
            or not accept_header.strip()
            or not content_type_description.strip()
        ):
            raise ValueError(
                "Content types, Accept header, and response description must be non-blank"
            )
        deadline = time.monotonic() + self.timeout
        for hop in range(self.max_redirects + 1):
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            ):
                raise ValueError("Only public HTTP(S) URLs without credentials are supported")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if port not in {80, 443}:
                raise ValueError("Only HTTP(S) ports 80 and 443 are supported")
            host = parsed.hostname.encode("idna").decode("ascii")
            if allowed_hosts is not None and host.lower() not in {
                allowed_host.encode("idna").decode("ascii").lower()
                for allowed_host in allowed_hosts
            }:
                raise ValueError(f"URL left the allowed host set: {host}")
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if not addresses or any(
                not _is_public_address(address[4][0]) for address in addresses
            ):
                raise ValueError("URL resolves to a non-public or unsupported address")

            connection = http.client.HTTPConnection(host, port)
            response: http.client.HTTPResponse | None = None
            try:
                # Connect to the validated IP; keep the original host for HTTP and TLS.
                connection.sock = socket.create_connection(
                    (addresses[0][4][0], port), timeout=self._remaining(deadline)
                )
                if parsed.scheme == "https":
                    connection.sock = ssl.create_default_context().wrap_socket(
                        connection.sock, server_hostname=host
                    )
                transport_socket = connection.sock
                path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
                if parsed.query:
                    path += "?" + quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~")
                connection.request(
                    "GET",
                    path,
                    headers={
                        # Explicit: HTTPConnection on port 443 would send
                        # "Host: name:443", which some servers reject.
                        "Host": host,
                        "User-Agent": self.user_agent,
                        "Accept": (
                            "text/plain"
                            if parsed.path.lower().rstrip("/").endswith("/robots.txt")
                            else accept_header
                        ),
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or hop == self.max_redirects:
                        raise RuntimeError("Invalid redirect or redirect limit exceeded")
                    url = urljoin(url, location)
                    continue
                is_robots_txt = parsed.path.lower().rstrip("/").endswith("/robots.txt")
                if is_robots_txt and response.status in {404, 410}:
                    return FetchedResource(
                        url=url,
                        body=b"",
                        fetched_at=datetime.now(timezone.utc),
                        content_type="text/plain",
                        status_code=response.status,
                    )
                if response.status != 200:
                    raise RuntimeError(f"Request failed with HTTP {response.status}")
                content_type = response.getheader("Content-Type", "").split(";")[0].strip().lower()
                accepted_types = (
                    accepted_content_types | {"text/plain"}
                    if is_robots_txt
                    else accepted_content_types
                )
                if content_type not in accepted_types:
                    raise ValueError(
                        f"URL did not return an accepted {content_type_description} "
                        f"content type: {content_type or 'missing'}"
                    )
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed responses are not supported")
                if response.length is not None and response.length > self.max_bytes:
                    raise ValueError("Response exceeds the download size limit")
                body = bytearray()
                while not response.isclosed():
                    transport_socket.settimeout(self._remaining(deadline))
                    chunk = response.read1(min(65536, self.max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ValueError("Response exceeds the download size limit")
                if response.length not in {None, 0}:
                    raise RuntimeError("Response ended before the declared content length")
                return FetchedResource(
                    url=url,
                    body=bytes(body),
                    fetched_at=datetime.now(timezone.utc),
                    content_type=content_type,
                    encoding=response.headers.get_content_charset(),
                    status_code=response.status,
                )
            finally:
                if response is not None:
                    response.close()
                connection.close()
        raise RuntimeError("Article redirect limit exceeded")

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Article download time limit exceeded")
        return remaining
