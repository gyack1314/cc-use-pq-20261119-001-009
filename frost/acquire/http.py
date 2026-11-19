from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from frost.domain import Fault, canonical_url, same_origin


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class FetchFailure(Fault):
    def __init__(self, code: str, message: str, retry_after: int | None = None):
        super().__init__(code, message, 502)
        self.retry_after = retry_after


def retry_delay(value: str | None, now: int) -> int | None:
    if not value:
        return None
    try:
        if value.isascii() and value.isdigit():
            delay = int(value)
        else:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                return None
            delay = int(moment.timestamp()) - now
        return min(86400, max(1, delay))
    except (ValueError, TypeError, OverflowError):
        return None


class HttpFetcher:
    def __init__(self, timeout: float = 8.0, allow_loopback: bool = False):
        self.timeout = timeout
        self.allow_loopback = allow_loopback

    def check_host(self, host: str, port: int):
        import ipaddress

        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise FetchFailure("dns_failure", "无法解析已授权来源") from exc
        if not addresses:
            raise FetchFailure("dns_failure", "来源没有可用地址")
        for entry in addresses:
            address = ipaddress.ip_address(entry[4][0])
            if not address.is_global and not (self.allow_loopback and address.is_loopback):
                raise FetchFailure("blocked_address", "来源地址不属于允许访问的网络")
        return addresses

    def get(self, url: str, headers: dict[str, str], max_bytes: int) -> Response:
        url = canonical_url(url)
        original = url
        for redirect in range(4):
            parts = urlsplit(url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            addresses = self.check_host(parts.hostname, port)
            connection = http.client.HTTPConnection(parts.hostname, port, timeout=self.timeout)
            try:
                family, socktype, protocol, _, address = addresses[0]
                sock = socket.socket(family, socktype, protocol)
                sock.settimeout(self.timeout)
                try:
                    sock.connect(address)
                    if parts.scheme == "https":
                        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
                    connection.sock = sock
                except BaseException:
                    sock.close()
                    raise
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                request_headers = {"User-Agent": "FrostBulletins/1.0", "Accept-Encoding": "identity"}
                request_headers.update(headers)
                connection.request("GET", path, headers=request_headers)
                response = connection.getresponse()
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                if response.status in {301, 302, 303, 307, 308}:
                    location = response_headers.get("location")
                    if not location or redirect == 3:
                        raise FetchFailure("redirect_limit", "来源重定向不可完成")
                    url = same_origin(original, location if "://" in location else same_origin(url, location))
                    continue
                encoding = response_headers.get("content-encoding", "identity").lower()
                if encoding != "identity":
                    raise FetchFailure("unsupported_encoding", "来源未遵守非压缩响应约定")
                length = response_headers.get("content-length")
                if length:
                    try:
                        if int(length) < 0 or int(length) > max_bytes:
                            raise FetchFailure("body_limit", "来源响应超过字节预算")
                    except ValueError as exc:
                        raise FetchFailure("invalid_length", "来源长度声明无效") from exc
                chunks = []
                remaining = max_bytes + 1
                while remaining:
                    chunk = response.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                body = b"".join(chunks)
                if len(body) > max_bytes:
                    raise FetchFailure("body_limit", "来源响应超过字节预算")
                return Response(url, response.status, response_headers, body)
            except (OSError, http.client.HTTPException) as exc:
                raise FetchFailure("transport_failure", "来源连接中断或响应不完整") from exc
            finally:
                connection.close()
        raise FetchFailure("redirect_limit", "来源重定向过多")
