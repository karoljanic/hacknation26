import io
import socket
import ssl
from unittest.mock import Mock

import pytest

from prawdowid.adapters.sources.web import PublicWebFetcher


class WireSocket:
    def __init__(self, response: bytes) -> None:
        self.response = io.BytesIO(response)
        self.sent = b""
        self.closed = False

    def makefile(self, mode: str) -> io.BytesIO:
        return self.response

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def close(self) -> None:
        self.closed = True


def resolve(address: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]


def test_fetch_pins_public_ip_and_keeps_http_host(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = WireSocket(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        b"Content-Length: 12\r\nConnection: close\r\n\r\n<p>Hello</p>"
    )
    connector = Mock(return_value=wire)
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve("8.8.8.8")))
    monkeypatch.setattr(socket, "create_connection", connector)

    article = PublicWebFetcher().fetch("http://news.example/article?q=1#ignored")
    assert connector.call_args.args[0] == ("8.8.8.8", 80)
    assert b"Host: news.example\r\n" in wire.sent
    assert b"GET /article?q=1 HTTP/1.1" in wire.sent
    assert article.body == b"<p>Hello</p>"
    assert article.encoding == "utf-8"
    assert wire.closed and wire.response.closed


def test_https_keeps_original_hostname_for_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = WireSocket(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 12\r\n\r\n<p>Hello</p>"
    )
    context = Mock(spec=ssl.SSLContext)
    context.wrap_socket.return_value = wire
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve("8.8.8.8")))
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=wire))
    monkeypatch.setattr(ssl, "create_default_context", Mock(return_value=context))
    PublicWebFetcher().fetch("https://news.example/")
    context.wrap_socket.assert_called_once_with(wire, server_hostname="news.example")
    assert wire.closed


def test_fetch_resource_accepts_bounded_json_from_an_allowlisted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"ok":true}'
    wire = WireSocket(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    connector = Mock(return_value=wire)
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve("8.8.8.8")))
    monkeypatch.setattr(socket, "create_connection", connector)

    resource = PublicWebFetcher().fetch_resource(
        "http://api.example/data",
        allowed_hosts={"api.example"},
        accepted_content_types={"application/json"},
        accept_header="application/json",
        content_type_description="JSON",
    )

    assert resource.body == body
    assert resource.content_type == "application/json"
    assert resource.status_code == 200
    assert b"Accept: application/json\r\n" in wire.sent
    assert connector.call_args.args[0] == ("8.8.8.8", 80)


def test_connection_close_response_can_be_read_with_a_real_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, server = socket.socketpair()
    with client, server:
        server.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Length: 12\r\nConnection: close\r\n\r\n<p>Hello</p>"
        )
        monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve("8.8.8.8")))
        monkeypatch.setattr(socket, "create_connection", Mock(return_value=client))
        assert PublicWebFetcher().fetch("http://news.example/").body == b"<p>Hello</p>"


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:8.8.8.8", "2002:0808:0808::1"],
)
def test_non_public_and_tunnel_addresses_are_rejected(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    connector = Mock()
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve(address)))
    monkeypatch.setattr(socket, "create_connection", connector)
    with pytest.raises(ValueError, match="non-public"):
        PublicWebFetcher().fetch("http://news.example/")
    connector.assert_not_called()


def test_redirect_to_internal_network_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = WireSocket(b"HTTP/1.1 302 Found\r\nLocation: http://internal.example/\r\n\r\n")
    connector = Mock(return_value=wire)
    monkeypatch.setattr(
        socket, "getaddrinfo", Mock(side_effect=[resolve("8.8.8.8"), resolve("192.168.0.1")])
    )
    monkeypatch.setattr(socket, "create_connection", connector)
    with pytest.raises(ValueError, match="non-public"):
        PublicWebFetcher().fetch("http://news.example/")
    assert connector.call_count == 1
    assert wire.closed and wire.response.closed


def test_redirect_outside_allowed_hosts_is_rejected_before_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = WireSocket(b"HTTP/1.1 302 Found\r\nLocation: http://other.example/\r\n\r\n")
    connector = Mock(return_value=wire)
    resolver = Mock(return_value=resolve("8.8.8.8"))
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    monkeypatch.setattr(socket, "create_connection", connector)

    with pytest.raises(ValueError, match="allowed host"):
        PublicWebFetcher().fetch("http://news.example/", allowed_hosts={"news.example"})

    assert resolver.call_count == 1
    assert connector.call_count == 1
    assert wire.closed and wire.response.closed


def test_robots_txt_accepts_plain_text_and_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        WireSocket(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: 25\r\nConnection: close\r\n\r\nUser-agent: *\nDisallow: /"
        ),
        WireSocket(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve("8.8.8.8")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=responses))
    fetcher = PublicWebFetcher()

    robots = fetcher.fetch("http://news.example/robots.txt")
    missing = fetcher.fetch("http://news.example/robots.txt")

    assert robots.body == b"User-agent: *\nDisallow: /"
    assert robots.status_code == 200
    assert missing.body == b""
    assert missing.status_code == 404
    assert all(wire.closed and wire.response.closed for wire in responses)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n\r\nx", "HTML"),
        (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n\r\nx", "Compressed"),
        (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n123456", "size limit"),
        (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 5\r\n\r\nx", "content length"),
        (b"HTTP/1.1 403 Forbidden\r\n\r\n", "HTTP 403"),
        (b"HTTP/1.1 302 Found\r\nLocation: /again\r\n\r\n", "redirect limit"),
    ],
)
def test_invalid_or_oversized_response_is_explicit_error(
    monkeypatch: pytest.MonkeyPatch, response: bytes, message: str
) -> None:
    wire = WireSocket(response)
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve("8.8.8.8")))
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=wire))
    with pytest.raises((ValueError, RuntimeError), match=message):
        PublicWebFetcher(max_bytes=5, max_redirects=0).fetch("http://news.example/")
    assert wire.closed and wire.response.closed


def test_fetch_resource_rejects_unexpected_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = WireSocket(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1\r\n\r\nx"
    )
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=resolve("8.8.8.8")))
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=wire))

    with pytest.raises(ValueError, match="structured data content type"):
        PublicWebFetcher().fetch_resource(
            "http://api.example/data",
            allowed_hosts={"api.example"},
            accepted_content_types={"application/json"},
            content_type_description="structured data",
        )


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://user:pass@news.example/", "http://news.example:8080/"]
)
def test_invalid_url_is_rejected_before_dns(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    resolver = Mock()
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    with pytest.raises(ValueError):
        PublicWebFetcher().fetch(url)
    resolver.assert_not_called()
