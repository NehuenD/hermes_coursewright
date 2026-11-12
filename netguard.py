"""Outbound URL guard for pages found by research, using only documented stdlib APIs.

A URL in a search result is attacker-influenced, not trusted. ``fetch`` checks its scheme,
resolves the hostname and checks every resolved address, then dials the validated address
itself. Redirects are followed manually and every hop goes through the same checks, so an
initially public URL cannot reach loopback, a private network or the cloud metadata service
through a redirect, an encoded IP form or a second DNS answer. SNI and certificate validation
still use the original hostname.
"""

from __future__ import annotations

import http.client
import ipaddress
import ssl
import socket
from typing import NamedTuple, Optional

DEFAULT_TIMEOUT = 12.0
MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 1000000
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class GuardError(Exception):
    """The target is not a URL this plugin is willing to fetch."""


class Target(NamedTuple):
    scheme: str
    host: str                 # ASCII, for resolution and as Host/SNI header
    port: int
    host_header: str
    request_target: str


def is_public_ip(text: str) -> bool:
    """False for anything private, loopback, link-local, reserved or not routable.

    IPv4-mapped IPv6 answers are unwrapped before the check, and CGNAT is explicit so this
    policy does not depend on which ranges a particular Python release calls private.
    The link-local class includes the cloud metadata addresses.
    """
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified or
                (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT))


def check_url(url: str) -> Target:
    """Parse an ordinary http(s) URL or raise ``GuardError`` without touching the network."""
    from urllib.parse import urlsplit

    url = str(url or "").strip()
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise GuardError("only http(s) URLs may be fetched")
        # Different clients parse backslashes, userinfo and percent escapes in authorities
        # differently. Refuse ambiguity rather than trusting this parser's opinion.
        if any(char in parsed.netloc for char in "@%\\"):
            raise GuardError("ambiguous URL authority")
        host = parsed.hostname
        if not host:
            raise GuardError("URL has no hostname")
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError:
            raise GuardError("invalid port")
        if not 1 <= port <= 65535:
            raise GuardError("invalid port")
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            raise GuardError("invalid hostname")
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query
        default_port = 443 if scheme == "https" else 80
        host_header = f"[{host}]" if ":" in host else host
        if port != default_port:
            host_header += ":" + str(port)
        return Target(scheme, host, port, host_header, request_path)
    except GuardError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise GuardError("could not parse URL") from exc


def resolve_checked(host: str, port: int) -> list[str]:
    """Resolve once and return public addresses only, rejecting *every* mixed answer.

    Rejecting a name with one public and one private answer prevents a resolver-based bypass.
    DNS failure fails closed; an empty answer is not a public host.
    """
    try:
        answers = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise GuardError("hostname could not be resolved") from exc
    addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
    if not addresses:
        raise GuardError("hostname has no address")
    if any(not is_public_ip(ip) for ip in addresses):
        raise GuardError("hostname resolves to a private/internal address")
    return addresses


def is_safe_url(url: str, resolver=resolve_checked) -> bool:
    """Convenience predicate when no page needs to be fetched (e.g. a scraper target)."""
    try:
        target = check_url(url)
        resolver(target.host, target.port)
        return True
    except (GuardError, OSError):
        return False


class _SocketClient:
    """Small HTTP/1.1 request against an address the caller already validated.

    Uses ``HTTPResponse`` for framing rather than reimplementing status lines, headers or
    chunked transfer encoding. No urllib, so redirects cannot sneak past the caller's loop.
    No private http.client transport attributes are patched.
    """

    def __init__(self, target: Target, ip: str, timeout: float, headers: dict, connector):
        self.target = target
        self.ip = ip
        self.timeout = timeout
        self.headers = headers
        self.connector = connector

    def get(self):
        sock = self.connector((self.ip, self.target.port), self.timeout)
        try:
            if self.target.scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=self.target.host)
            fields = {"Connection": "close", "Accept-Encoding": "identity", **self.headers,
                      "Host": self.target.host_header}
            lines = [f"GET {self.target.request_target} HTTP/1.1"]
            lines.extend(f"{key}: {value}" for key, value in fields.items())
            request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
            if b"\r" in request.replace(b"\r\n", b"") or b"\n" in request.replace(b"\r\n", b""):
                raise GuardError("invalid characters in request")
            sock.sendall(request)
            response = http.client.HTTPResponse(sock, method="GET")
            response.begin()
            return response
        except GuardError:
            sock.close()
            raise
        except Exception:
            sock.close()
            raise


def _default_connector(address, timeout):
    return socket.create_connection(address, timeout=timeout)


def fetch(url: str, timeout: float = DEFAULT_TIMEOUT, headers: Optional[dict] = None,
          max_bytes: int = DEFAULT_MAX_BYTES, max_redirects: int = MAX_REDIRECTS,
          resolver=resolve_checked, connector=None):
    """Return ``(status, headers, body_bytes)`` for a guarded HTTP request.

    ``resolver`` and ``connector`` are injectable for offline tests. At most ``max_redirects``
    redirects are followed; a redirect to another hop is not fetched before it is checked.
    """
    from urllib.parse import urljoin

    connector = connector or _default_connector
    current = str(url or "")
    for hop in range(max_redirects + 1):
        target = check_url(current)
        addresses = resolver(target.host, target.port)
        # Even a caller-supplied resolver's answers get checked: no code path may turn a
        # private destination into a socket merely by supplying its own DNS result.
        if not addresses or any(not is_public_ip(ip) for ip in addresses):
            raise GuardError("target address is private or unresolved")
        response = _SocketClient(target, addresses[0], timeout, headers or {}, connector).get()
        try:
            if (300 <= response.status < 400 and response.status != 304 and
                    response.headers.get("Location")):
                if hop >= max_redirects:
                    raise GuardError("too many redirects")
                # Resolution and validation happen at the top of the next iteration. A hop's
                # Location never flows directly to a socket.
                current = urljoin(current, response.headers["Location"])
                continue
            return response.status, response.headers, response.read(max_bytes)
        finally:
            response.close()
    raise GuardError("too many redirects")
