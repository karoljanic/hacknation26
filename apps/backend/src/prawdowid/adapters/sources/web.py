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
        self, *, timeout: float = 10, max_bytes: int = 2_000_000, max_redirects: int = 3
    ) -> None:
        if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
            raise ValueError("Invalid fetch limits")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    def fetch(self, url: str) -> FetchedArticle:
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
                        "User-Agent": "Prawdowid/0.1 (article extraction)",
                        "Accept": "text/html, application/xhtml+xml",
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
                if response.status != 200:
                    raise RuntimeError(f"Article request failed with HTTP {response.status}")
                content_type = response.getheader("Content-Type", "").split(";")[0].strip().lower()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise ValueError("URL did not return an HTML article")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed article responses are not supported")
                if response.length is not None and response.length > self.max_bytes:
                    raise ValueError("Article exceeds the download size limit")
                body = bytearray()
                while not response.isclosed():
                    transport_socket.settimeout(self._remaining(deadline))
                    chunk = response.read1(min(65536, self.max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ValueError("Article exceeds the download size limit")
                if response.length not in {None, 0}:
                    raise RuntimeError("Article response ended before the declared content length")
                return FetchedArticle(
                    url=url,
                    body=bytes(body),
                    fetched_at=datetime.now(timezone.utc),
                    encoding=response.headers.get_content_charset(),
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
