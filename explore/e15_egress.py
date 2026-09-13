"""Host-enforced public-egress boundary for E15 V12 evolution.

The desktop Agent is root inside a QEMU guest.  Guest firewall rules therefore
provide no security boundary.  This module installs the policy one layer out,
in the OSWorld Docker container which owns QEMU's ``dockerbridge`` tap, and
relays the one task-visible website through a filtering gateway in the trusted
host process.

Target mode permits only HTTP whose ``Host`` and HTTPS whose TLS SNI are exactly
``streamview.web.hku.icu``.  The gateway always resolves and connects that
canonical hostname itself; the guest cannot select an upstream by using a
different destination IP.  Null-project mode permits no guest-initiated public
traffic.  Existing host-controller connections are preserved with a narrow
ESTABLISHED/RELATED rule.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import ipaddress
import json
import logging
import socket
import ssl
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


log = logging.getLogger("forge.e15.egress")

ALLOWED_STREAMVIEW_HOST = "streamview.web.hku.icu"
POLICY_VERSION = "e15-v12-host-egress-seal-v3"
GUEST_BRIDGE = "dockerbridge"
NAT_CHAIN = "E15_V12_NAT"
FORWARD_CHAIN = "E15_V12_FWD"
INPUT_CHAIN = "E15_V12_IN"
OUTPUT_CHAIN = "E15_V12_OUT"
L2_CHAIN = "E15_V12_L2"
MAX_PREFACE_BYTES = 65_536


class E15EgressError(RuntimeError):
    """The host-enforced egress boundary could not be installed or verified."""


class PrefaceIncomplete(ValueError):
    """More bytes are required to decide whether a connection is allowed."""


class PrefaceRejected(ValueError):
    """An HTTP Host or TLS SNI is absent, malformed, or outside the allowlist."""


def _canonical_dns_name(value: str) -> str:
    """Return a strict ASCII DNS name without accepting lookalike spellings."""

    if not isinstance(value, str):
        raise PrefaceRejected("hostname is not text")
    if not value or value != value.strip() or value.endswith("."):
        raise PrefaceRejected("hostname is empty, padded, or has a trailing dot")
    try:
        encoded = value.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise PrefaceRejected("hostname is not ASCII") from exc
    lowered = encoded.decode("ascii").lower()
    labels = lowered.split(".")
    if any(
            not label or len(label) > 63
            or label[0] == "-" or label[-1] == "-"
            or any(not (character.isalnum() or character == "-")
                   for character in label)
            for label in labels):
        raise PrefaceRejected("hostname is not a canonical DNS name")
    return lowered


def _parse_http_authority(value: str, *, expected_port: int) -> str:
    """Parse a Host/URI authority, accepting only an optional matching port."""

    if "\r" in value or "\n" in value:
        raise PrefaceRejected("HTTP authority contains a line break")
    value = value.strip(" \t")
    if not value or "@" in value or "[" in value:
        raise PrefaceRejected("HTTP authority is malformed")
    if ":" in value:
        host, separator, port_text = value.rpartition(":")
        if not separator or not port_text.isdigit() or int(port_text) != expected_port:
            raise PrefaceRejected("HTTP authority uses an unexpected port")
    else:
        host = value
    return _canonical_dns_name(host)


def validate_http_preface(
        data: bytes, allowed_host: str = ALLOWED_STREAMVIEW_HOST, *,
        expected_port: int = 80, expected_scheme: str = "http") -> str:
    """Validate one complete HTTP request header and return its canonical Host.

    The upstream destination is never taken from this request; callers connect
    the configured ``allowed_host``.  Absolute-form request targets are checked
    independently so a proxy-style request cannot smuggle another authority.
    """

    marker = data.find(b"\r\n\r\n")
    if marker < 0:
        if len(data) > MAX_PREFACE_BYTES:
            raise PrefaceRejected("HTTP header exceeds the egress-gateway limit")
        raise PrefaceIncomplete("HTTP header is incomplete")
    if marker + 4 > MAX_PREFACE_BYTES:
        raise PrefaceRejected("HTTP header exceeds the egress-gateway limit")
    try:
        header = data[:marker].decode("iso-8859-1", "strict")
    except UnicodeDecodeError as exc:  # pragma: no cover - latin-1 is total
        raise PrefaceRejected("HTTP header is not decodable") from exc
    unfolded = header.replace("\r\n", "")
    if ("\x00" in header or "\r" in unfolded or "\n" in unfolded
            or any(ord(character) < 32 and character != "\t"
                   for character in unfolded)
            or "\x7f" in unfolded):
        raise PrefaceRejected("HTTP header contains invalid controls")
    lines = header.split("\r\n")
    if (not lines or "\t" in lines[0]
            or len(lines[0].split(" ")) != 3):
        raise PrefaceRejected("HTTP request line is malformed")
    method, target, version = lines[0].split(" ")
    if (not method or not method.isascii() or not method.isalpha()
            or version not in {"HTTP/1.0", "HTTP/1.1"}
            or method.upper() == "CONNECT"):
        raise PrefaceRejected("HTTP method or version is not allowed")

    expected = _canonical_dns_name(allowed_host)
    hosts: list[str] = []
    for line in lines[1:]:
        if not line or line[0] in " \t" or ":" not in line:
            raise PrefaceRejected("HTTP header line is malformed or folded")
        name, value = line.split(":", 1)
        if (not name or not name.isascii()
                or any(not (character.isalnum() or character == "-")
                       for character in name)):
            raise PrefaceRejected("HTTP header name is malformed")
        if name.lower() == "host":
            hosts.append(_parse_http_authority(
                value, expected_port=expected_port))
    if len(hosts) != 1 or hosts[0] != expected:
        raise PrefaceRejected("HTTP Host is absent, duplicated, or not allowed")

    if target.startswith(("http://", "https://")):
        parsed = urlsplit(target)
        if (parsed.scheme != expected_scheme or not parsed.netloc
                or parsed.username is not None or parsed.password is not None):
            raise PrefaceRejected("absolute HTTP target is malformed")
        try:
            target_port = parsed.port or expected_port
        except ValueError as exc:
            raise PrefaceRejected("absolute HTTP target port is malformed") from exc
        if (target_port != expected_port
                or _canonical_dns_name(parsed.hostname or "") != expected):
            raise PrefaceRejected("absolute HTTP target is not allowed")
    elif target != "*" and not target.startswith("/"):
        raise PrefaceRejected("HTTP target form is not allowed")
    return expected


@dataclass(frozen=True)
class PreparedHttpRequest:
    header: bytes
    content_length: int
    initial_body: bytes


def prepare_single_http_request(
        data: bytes, allowed_host: str = ALLOWED_STREAMVIEW_HOST, *,
        expected_port: int = 80,
        expected_scheme: str = "http") -> PreparedHttpRequest:
    """Frame and normalize exactly one HTTP/1.x request.

    Transfer coding, protocol upgrades, ``Expect``, duplicated Content-Length,
    and bytes beyond the declared body are rejected. The sole Host is rewritten
    to the canonical origin and ``Connection: close`` is forced, so the relay
    never becomes a reusable tunnel to another virtual host.
    """

    expected = validate_http_preface(
        data, allowed_host, expected_port=expected_port,
        expected_scheme=expected_scheme)
    marker = data.find(b"\r\n\r\n")
    header_end = marker + 4
    header_text = data[:marker].decode("iso-8859-1")
    lines = header_text.split("\r\n")
    method, target, version = lines[0].split(" ")
    if target.startswith(("http://", "https://")):
        parsed = urlsplit(target)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query

    content_lengths: list[int] = []
    normalized: list[str] = [f"{method} {target} {version}"]
    for line in lines[1:]:
        name, value = line.split(":", 1)
        lowered = name.lower()
        stripped = value.strip(" \t")
        if lowered == "host":
            continue
        if lowered == "content-length":
            if not stripped.isdigit():
                raise PrefaceRejected("Content-Length is not a decimal integer")
            content_lengths.append(int(stripped))
            continue
        if lowered in {
                "transfer-encoding", "expect", "upgrade", "te",
                "trailer"}:
            raise PrefaceRejected(
                f"HTTP request uses forbidden framing header {name}")
        if lowered in {"connection", "proxy-connection", "keep-alive"}:
            continue
        if lowered in {
                "forwarded", "x-forwarded-host", "x-forwarded-server",
                "x-original-host"}:
            continue
        normalized.append(f"{name}: {stripped}")
    if len(content_lengths) > 1:
        raise PrefaceRejected("HTTP request has ambiguous Content-Length")
    content_length = content_lengths[0] if content_lengths else 0
    initial_body = data[header_end:]
    if len(initial_body) > content_length:
        raise PrefaceRejected(
            "HTTP connection contains bytes beyond its sole declared request")
    host_value = expected if expected_port in {80, 443} else \
        f"{expected}:{expected_port}"
    normalized.append(f"Host: {host_value}")
    if content_lengths:
        normalized.append(f"Content-Length: {content_length}")
    normalized.append("Connection: close")
    header = ("\r\n".join(normalized) + "\r\n\r\n").encode(
        "iso-8859-1", "strict")
    return PreparedHttpRequest(
        header=header, content_length=content_length,
        initial_body=initial_body)


def _take(data: bytes, offset: int, size: int) -> tuple[bytes, int]:
    end = offset + size
    if size < 0 or end > len(data):
        raise PrefaceRejected("TLS ClientHello is truncated internally")
    return data[offset:end], end


def _parse_client_hello_body(body: bytes) -> str:
    """Extract the sole host_name entry from a complete ClientHello body."""

    offset = 0
    _, offset = _take(body, offset, 2 + 32)  # legacy_version + random
    session_size_b, offset = _take(body, offset, 1)
    _, offset = _take(body, offset, session_size_b[0])
    cipher_size_b, offset = _take(body, offset, 2)
    cipher_size = int.from_bytes(cipher_size_b, "big")
    if cipher_size == 0 or cipher_size % 2:
        raise PrefaceRejected("TLS cipher-suite vector is malformed")
    _, offset = _take(body, offset, cipher_size)
    compression_size_b, offset = _take(body, offset, 1)
    if compression_size_b[0] == 0:
        raise PrefaceRejected("TLS compression vector is empty")
    _, offset = _take(body, offset, compression_size_b[0])
    extensions_size_b, offset = _take(body, offset, 2)
    extensions_size = int.from_bytes(extensions_size_b, "big")
    extensions, offset = _take(body, offset, extensions_size)
    if offset != len(body):
        raise PrefaceRejected("TLS ClientHello has trailing bytes")

    sni_extensions = 0
    host_names: list[str] = []
    ext_offset = 0
    while ext_offset < len(extensions):
        ext_type_b, ext_offset = _take(extensions, ext_offset, 2)
        ext_size_b, ext_offset = _take(extensions, ext_offset, 2)
        ext_size = int.from_bytes(ext_size_b, "big")
        ext_data, ext_offset = _take(extensions, ext_offset, ext_size)
        if int.from_bytes(ext_type_b, "big") != 0:
            continue
        sni_extensions += 1
        if len(ext_data) < 2:
            raise PrefaceRejected("TLS SNI extension is malformed")
        names_size = int.from_bytes(ext_data[:2], "big")
        if names_size != len(ext_data) - 2:
            raise PrefaceRejected("TLS SNI list length is malformed")
        name_offset = 2
        while name_offset < len(ext_data):
            name_type_b, name_offset = _take(ext_data, name_offset, 1)
            name_size_b, name_offset = _take(ext_data, name_offset, 2)
            name_size = int.from_bytes(name_size_b, "big")
            name_b, name_offset = _take(ext_data, name_offset, name_size)
            if name_type_b[0] == 0:
                try:
                    host_names.append(name_b.decode("ascii", "strict"))
                except UnicodeDecodeError as exc:
                    raise PrefaceRejected("TLS SNI host is not ASCII") from exc
    if ext_offset != len(extensions):  # defensive; _take normally catches it
        raise PrefaceRejected("TLS extension vector is malformed")
    if sni_extensions != 1 or len(host_names) != 1:
        raise PrefaceRejected("TLS requires exactly one SNI host_name")
    return _canonical_dns_name(host_names[0])


def parse_tls_client_hello_sni(data: bytes) -> str:
    """Parse TLS records through a complete ClientHello and return exact SNI."""

    if len(data) > MAX_PREFACE_BYTES:
        raise PrefaceRejected("TLS ClientHello exceeds the gateway limit")
    records_offset = 0
    handshake = bytearray()
    expected_handshake_size: int | None = None
    while True:
        if len(data) - records_offset < 5:
            raise PrefaceIncomplete("TLS record header is incomplete")
        content_type = data[records_offset]
        legacy_major = data[records_offset + 1]
        record_size = int.from_bytes(
            data[records_offset + 3:records_offset + 5], "big")
        if content_type != 22 or legacy_major != 3 or record_size > 18_432:
            raise PrefaceRejected("connection does not begin with TLS handshake records")
        record_end = records_offset + 5 + record_size
        if record_end > len(data):
            raise PrefaceIncomplete("TLS record body is incomplete")
        handshake.extend(data[records_offset + 5:record_end])
        records_offset = record_end
        if len(handshake) >= 4 and expected_handshake_size is None:
            if handshake[0] != 1:
                raise PrefaceRejected("first TLS handshake is not ClientHello")
            expected_handshake_size = 4 + int.from_bytes(handshake[1:4], "big")
            if expected_handshake_size > MAX_PREFACE_BYTES:
                raise PrefaceRejected("TLS ClientHello is too large")
        if (expected_handshake_size is not None
                and len(handshake) >= expected_handshake_size):
            return _parse_client_hello_body(
                bytes(handshake[4:expected_handshake_size]))
        if records_offset == len(data):
            raise PrefaceIncomplete("TLS ClientHello spans another record")


def validate_tls_preface(
        data: bytes, allowed_host: str = ALLOWED_STREAMVIEW_HOST) -> str:
    host = parse_tls_client_hello_sni(data)
    if host != _canonical_dns_name(allowed_host):
        raise PrefaceRejected("TLS SNI is not allowed")
    return host


@dataclass(frozen=True)
class GatewayPorts:
    http: int
    https: int


@dataclass(frozen=True)
class GatewayIdentity:
    """Ephemeral CA/leaf identity retained only by the trusted host process."""

    ca_pem: bytes
    leaf_pem: bytes
    private_key_pem: bytes
    leaf_spki_sha256: str
    leaf_spki_sha256_b64: str


def _new_gateway_identity(hostname: str) -> GatewayIdentity:
    """Create a short-lived CA and exact-host leaf for TLS interception."""

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    canonical = _canonical_dns_name(hostname)
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "E15 V12 ephemeral egress CA"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False), True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, canonical),
    ])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(canonical)]), False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=True, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False), True)
        .sign(ca_key, hashes.SHA256())
    )
    spki = leaf_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    return GatewayIdentity(
        ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM),
        leaf_pem=leaf_cert.public_bytes(serialization.Encoding.PEM),
        private_key_pem=leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()),
        leaf_spki_sha256=hashlib.sha256(spki).hexdigest(),
        leaf_spki_sha256_b64=base64.b64encode(
            hashlib.sha256(spki).digest()).decode("ascii"),
    )


def _server_ssl_context(
        identity: GatewayIdentity, allowed_host: str) -> ssl.SSLContext:
    """Load the ephemeral key without leaving it on disk after setup."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    with tempfile.TemporaryDirectory(prefix="e15-egress-cert-") as temp:
        cert_path = Path(temp) / "leaf.pem"
        key_path = Path(temp) / "leaf.key"
        cert_path.write_bytes(identity.leaf_pem)
        key_path.write_bytes(identity.private_key_pem)
        key_path.chmod(0o600)
        context.load_cert_chain(str(cert_path), str(key_path))

    expected = _canonical_dns_name(allowed_host)

    def require_exact_sni(
            _socket: ssl.SSLSocket, server_name: str | None,
            _context: ssl.SSLContext) -> int | None:
        try:
            observed = _canonical_dns_name(server_name or "")
        except PrefaceRejected:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        if observed != expected:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        return None

    context.set_servername_callback(require_exact_sni)
    return context


class FilteringTcpGateway:
    """Host-side HTTP/TLS terminator enforcing exact origin at both layers."""

    def __init__(
            self, bind_ip: str, allowed_host: str = ALLOWED_STREAMVIEW_HOST,
            *, connect: Callable[..., socket.socket] = socket.create_connection,
            max_connections: int = 64, handshake_timeout: float = 10.0,
            identity: GatewayIdentity | None = None,
            upstream_ips: tuple[str, ...] | None = None) -> None:
        ipaddress.ip_address(bind_ip)
        self.bind_ip = bind_ip
        self.allowed_host = _canonical_dns_name(allowed_host)
        self._connect = connect
        self._handshake_timeout = handshake_timeout
        self.upstream_ips = upstream_ips or self._resolve_upstream_ips()
        self.identity = identity or _new_gateway_identity(self.allowed_host)
        self._server_context = _server_ssl_context(
            self.identity, self.allowed_host)
        self._upstream_context = ssl.create_default_context()
        self._upstream_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._upstream_context.set_alpn_protocols(["http/1.1"])
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stop = threading.Event()
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._ports: GatewayPorts | None = None

    def _resolve_upstream_ips(self) -> tuple[str, ...]:
        """Resolve once in trusted host context; Agents never choose an IP."""

        try:
            infos = socket.getaddrinfo(
                self.allowed_host, 443, socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            raise E15EgressError(
                "could not resolve the exact allowed StreamView origin") from exc
        addresses = tuple(sorted({str(info[4][0]) for info in infos}))
        if not addresses:
            raise E15EgressError("allowed StreamView origin has no IPv4 address")
        for text_ip in addresses:
            address = ipaddress.ip_address(text_ip)
            if address.version != 4 or not address.is_global:
                raise E15EgressError(
                    "allowed StreamView resolved outside public IPv4")
        return addresses

    @property
    def ports(self) -> GatewayPorts:
        if self._ports is None:
            raise E15EgressError("filtering gateway has not started")
        return self._ports

    def start(self) -> GatewayPorts:
        if self._ports is not None:
            return self._ports
        listeners: list[socket.socket] = []
        try:
            for _protocol in ("http", "https"):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.bind_ip, 0))
                listener.listen(128)
                listener.settimeout(0.5)
                listeners.append(listener)
        except Exception:
            for listener in listeners:
                listener.close()
            raise
        self._listeners = listeners
        self._ports = GatewayPorts(
            http=listeners[0].getsockname()[1],
            https=listeners[1].getsockname()[1])
        for listener, protocol in zip(listeners, ("http", "https")):
            thread = threading.Thread(
                target=self._accept_loop, args=(listener, protocol),
                name=f"e15-egress-{protocol}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self._ports

    def close(self) -> None:
        self._stop.set()
        for listener in self._listeners:
            try:
                listener.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)
        self._listeners.clear()
        self._threads.clear()
        self._ports = None

    def __enter__(self) -> "FilteringTcpGateway":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _accept_loop(self, listener: socket.socket, protocol: str) -> None:
        while not self._stop.is_set():
            try:
                client, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            thread = threading.Thread(
                target=self._handle_guarded,
                args=(client, protocol), daemon=True,
                name=f"e15-egress-{protocol}-connection")
            thread.start()

    def _handle_guarded(self, client: socket.socket, protocol: str) -> None:
        try:
            self._handle(client, protocol)
        except (OSError, PrefaceIncomplete, PrefaceRejected) as exc:
            log.debug("E15 egress gateway rejected %s connection: %s",
                      protocol, exc)
            if protocol == "http":
                try:
                    client.sendall(
                        b"HTTP/1.1 421 Misdirected Request\r\n"
                        b"Connection: close\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
        finally:
            try:
                client.close()
            finally:
                self._slots.release()

    def _read_request_head(
            self, client: socket.socket,
            protocol: str) -> PreparedHttpRequest:
        payload = bytearray()
        while len(payload) <= MAX_PREFACE_BYTES:
            chunk = client.recv(min(8192, MAX_PREFACE_BYTES + 1 - len(payload)))
            if not chunk:
                raise PrefaceRejected("connection closed before an allowed preface")
            payload.extend(chunk)
            try:
                return prepare_single_http_request(
                    bytes(payload), self.allowed_host,
                    expected_port=(80 if protocol == "http" else 443),
                    expected_scheme=protocol)
            except PrefaceIncomplete:
                continue
        raise PrefaceRejected("connection preface exceeds the gateway limit")

    def _handle(self, client: socket.socket, protocol: str) -> None:
        client.settimeout(self._handshake_timeout)
        downstream: socket.socket = client
        if protocol == "https":
            # Termination is required: SNI-only forwarding would allow an
            # encrypted request carrying a different HTTP Host to a co-tenant.
            downstream = self._server_context.wrap_socket(
                client, server_side=True)
            downstream.settimeout(self._handshake_timeout)
        request = self._read_request_head(downstream, protocol)
        upstream_port = 80 if protocol == "http" else 443
        raw_upstream: socket.socket | None = None
        last_error: OSError | None = None
        for upstream_ip in self.upstream_ips:
            try:
                raw_upstream = self._connect(
                    (upstream_ip, upstream_port), self._handshake_timeout)
                break
            except OSError as exc:
                last_error = exc
        if raw_upstream is None:
            raise last_error or OSError("no pinned StreamView upstream")
        upstream: socket.socket = raw_upstream
        if protocol == "https":
            try:
                upstream = self._upstream_context.wrap_socket(
                    raw_upstream, server_hostname=self.allowed_host)
            except Exception:
                raw_upstream.close()
                raise
        try:
            upstream.sendall(request.header)
            if request.initial_body:
                upstream.sendall(request.initial_body)
            remaining = request.content_length - len(request.initial_body)
            while remaining:
                chunk = downstream.recv(min(65_536, remaining))
                if not chunk:
                    raise PrefaceRejected(
                        "HTTP request body ended before Content-Length")
                upstream.sendall(chunk)
                remaining -= len(chunk)
            # Never read from the downstream again. A later keep-alive or
            # pipelined request therefore cannot cross this one-request proxy.
            downstream.settimeout(None)
            upstream.settimeout(120.0)
            self._relay_one_response(downstream, upstream)
        finally:
            upstream.close()
            if downstream is not client:
                downstream.close()

    def _relay_one_response(
            self, client: socket.socket, upstream: socket.socket) -> None:
        while not self._stop.is_set():
            data = upstream.recv(65_536)
            if not data:
                return
            client.sendall(data)


def egress_policy_manifest(
        allowed_host: str = ALLOWED_STREAMVIEW_HOST) -> dict[str, Any]:
    """Stable, port-independent provenance for the enforced network policy."""

    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "enforcement_boundary": "outer-osworld-container-plus-host-gateway",
        "guest_bridge": GUEST_BRIDGE,
        "target_public_egress": {
            "host": _canonical_dns_name(allowed_host),
            "protocols": [
                "http-host-exact",
                "https-terminated-sni-exact-and-inner-host-exact",
                "one-framed-request-per-connection-canonical-host-close",
            ],
            "ports": [80, 443],
        },
        "null_project_public_egress": [],
        "blocked": [
            "guest-dns", "direct-ip-bypass", "non-http-tcp", "udp",
            "ipv6", "outer-container-services",
        ],
        "host_controller_return_traffic": "established-related-only",
        "outer_container_output": [
            "loopback", "dhcp-reply-to-guest", "conntrack-reply-only",
        ],
        "topology_attestation": [
            "qemu-netdev-tap-ifname-qemu",
            "qemu-enslaved-to-dockerbridge",
            "layer2-ipv6-drop",
        ],
        "ipv6_enforcement_modes": [
            "ip6tables-plus-ebtables",
            "ebtables-authoritative-no-outer-ipv6",
        ],
    }


def egress_policy_sha256(
        allowed_host: str = ALLOWED_STREAMVIEW_HOST) -> str:
    payload = json.dumps(
        egress_policy_manifest(allowed_host), sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _exec_result(result: Any) -> tuple[int, str]:
    if hasattr(result, "exit_code"):
        code, output = int(result.exit_code), getattr(result, "output", b"")
    elif isinstance(result, tuple) and len(result) == 2:
        code, output = int(result[0]), result[1]
    else:
        raise E15EgressError("outer-container exec returned an unknown result")
    if isinstance(output, bytes):
        text = output.decode("utf-8", "replace")
    else:
        text = str(output or "")
    return code, text


def _validated_gateway_endpoint(gateway_ip: str, ports: GatewayPorts) -> None:
    address = ipaddress.ip_address(gateway_ip)
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise E15EgressError("host gateway is not a usable IPv4 address")
    for port in (ports.http, ports.https):
        if not isinstance(port, int) or not 1 <= port <= 65_535:
            raise E15EgressError("host gateway port is invalid")


def _firewall_script(
        *, mode: str, gateway_ip: str | None = None,
        ports: GatewayPorts | None = None) -> str:
    """Return an idempotent fail-closed ruleset installer/checker."""

    if mode not in {"target", "null"}:
        raise E15EgressError("egress mode must be target or null")
    if mode == "target":
        if gateway_ip is None or ports is None:
            raise E15EgressError("target egress requires a host gateway")
        _validated_gateway_endpoint(gateway_ip, ports)
        http_rule = (
            f"iptables -w -t nat -A {NAT_CHAIN} -p tcp --dport 80 "
            f"-j DNAT --to-destination {gateway_ip}:{ports.http}")
        https_rule = (
            f"iptables -w -t nat -A {NAT_CHAIN} -p tcp --dport 443 "
            f"-j DNAT --to-destination {gateway_ip}:{ports.https}")
        forward_rules = f"""
iptables -w -A {FORWARD_CHAIN} -p tcp -d {gateway_ip} --dport {ports.http} -j ACCEPT
iptables -w -A {FORWARD_CHAIN} -p tcp -d {gateway_ip} --dport {ports.https} -j ACCEPT
"""
        target_checks = f"""
iptables -w -t nat -C {NAT_CHAIN} -p tcp --dport 80 -j DNAT --to-destination {gateway_ip}:{ports.http}
iptables -w -t nat -C {NAT_CHAIN} -p tcp --dport 443 -j DNAT --to-destination {gateway_ip}:{ports.https}
iptables -w -C {FORWARD_CHAIN} -p tcp -d {gateway_ip} --dport {ports.http} -j ACCEPT
iptables -w -C {FORWARD_CHAIN} -p tcp -d {gateway_ip} --dport {ports.https} -j ACCEPT
"""
    else:
        http_rule = https_rule = ""
        forward_rules = ""
        target_checks = ""

    return f"""
set -eu
test -d /sys/class/net/{GUEST_BRIDGE}
qemu_pid=$(pgrep -f '^qemu-system-x86_64 ' || true)
test -n "$qemu_pid"
test "$(printf '%s\n' "$qemu_pid" | wc -l)" -eq 1
tr '\\000' ' ' < "/proc/$qemu_pid/cmdline" | \
  grep -Eq -- '(^| )-netdev tap,[^ ]*ifname=qemu(,| )'
test "$(basename "$(readlink -f /sys/class/net/qemu/master)")" = \
  '{GUEST_BRIDGE}'

set +e
e15_ip6_probe=$(ip6tables -w -S 2>&1)
e15_ip6_rc=$?
set -e
if test "$e15_ip6_rc" -eq 0; then
  e15_ipv6_mode=ip6tables-plus-ebtables
elif test "$e15_ip6_rc" -eq 3 && \
     printf '%s' "$e15_ip6_probe" | grep -Fq 'Table does not exist'; then
  e15_ipv6_mode=ebtables-authoritative-no-outer-ipv6
  test -z "$(ip -6 -o addr show scope global)"
  test -z "$(ip -6 route show default)"
else
  printf 'unexpected ip6tables capability failure rc=%s: %s\n' \
    "$e15_ip6_rc" "$e15_ip6_probe" >&2
  exit 73
fi

iptables -w -t nat -N {NAT_CHAIN} 2>/dev/null || true
iptables -w -t nat -F {NAT_CHAIN}
while iptables -w -t nat -C PREROUTING -i {GUEST_BRIDGE} -j {NAT_CHAIN} 2>/dev/null; do
  iptables -w -t nat -D PREROUTING -i {GUEST_BRIDGE} -j {NAT_CHAIN}
done
iptables -w -t nat -I PREROUTING 1 -i {GUEST_BRIDGE} -j {NAT_CHAIN}
{http_rule}
{https_rule}

iptables -w -N {FORWARD_CHAIN} 2>/dev/null || true
iptables -w -F {FORWARD_CHAIN}
while iptables -w -C FORWARD -i {GUEST_BRIDGE} -j {FORWARD_CHAIN} 2>/dev/null; do
  iptables -w -D FORWARD -i {GUEST_BRIDGE} -j {FORWARD_CHAIN}
done
iptables -w -I FORWARD 1 -i {GUEST_BRIDGE} -j {FORWARD_CHAIN}
iptables -w -A {FORWARD_CHAIN} -m conntrack \
  --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
{forward_rules}
iptables -w -A {FORWARD_CHAIN} -j DROP

iptables -w -N {INPUT_CHAIN} 2>/dev/null || true
iptables -w -F {INPUT_CHAIN}
while iptables -w -C INPUT -i {GUEST_BRIDGE} -j {INPUT_CHAIN} 2>/dev/null; do
  iptables -w -D INPUT -i {GUEST_BRIDGE} -j {INPUT_CHAIN}
done
iptables -w -I INPUT 1 -i {GUEST_BRIDGE} -j {INPUT_CHAIN}
iptables -w -A {INPUT_CHAIN} -p udp --sport 68 --dport 67 -j ACCEPT
iptables -w -A {INPUT_CHAIN} -m conntrack \
  --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
iptables -w -A {INPUT_CHAIN} -j DROP

iptables -w -N {OUTPUT_CHAIN} 2>/dev/null || true
iptables -w -F {OUTPUT_CHAIN}
while iptables -w -C OUTPUT -j {OUTPUT_CHAIN} 2>/dev/null; do
  iptables -w -D OUTPUT -j {OUTPUT_CHAIN}
done
iptables -w -I OUTPUT 1 -j {OUTPUT_CHAIN}
iptables -w -A {OUTPUT_CHAIN} -o lo -j ACCEPT
iptables -w -A {OUTPUT_CHAIN} -o {GUEST_BRIDGE} -p udp \
  --sport 67 --dport 68 -j ACCEPT
iptables -w -A {OUTPUT_CHAIN} -m conntrack \
  --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
iptables -w -A {OUTPUT_CHAIN} -j DROP

if test "$e15_ipv6_mode" = ip6tables-plus-ebtables; then
ip6tables -w -N {FORWARD_CHAIN} 2>/dev/null || true
ip6tables -w -F {FORWARD_CHAIN}
while ip6tables -w -C FORWARD -i {GUEST_BRIDGE} -j {FORWARD_CHAIN} 2>/dev/null; do
  ip6tables -w -D FORWARD -i {GUEST_BRIDGE} -j {FORWARD_CHAIN}
done
ip6tables -w -I FORWARD 1 -i {GUEST_BRIDGE} -j {FORWARD_CHAIN}
ip6tables -w -A {FORWARD_CHAIN} -m conntrack \
  --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
ip6tables -w -A {FORWARD_CHAIN} -j DROP

ip6tables -w -N {INPUT_CHAIN} 2>/dev/null || true
ip6tables -w -F {INPUT_CHAIN}
while ip6tables -w -C INPUT -i {GUEST_BRIDGE} -j {INPUT_CHAIN} 2>/dev/null; do
  ip6tables -w -D INPUT -i {GUEST_BRIDGE} -j {INPUT_CHAIN}
done
ip6tables -w -I INPUT 1 -i {GUEST_BRIDGE} -j {INPUT_CHAIN}
ip6tables -w -A {INPUT_CHAIN} -m conntrack \
  --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
ip6tables -w -A {INPUT_CHAIN} -j DROP

ip6tables -w -N {OUTPUT_CHAIN} 2>/dev/null || true
ip6tables -w -F {OUTPUT_CHAIN}
while ip6tables -w -C OUTPUT -j {OUTPUT_CHAIN} 2>/dev/null; do
  ip6tables -w -D OUTPUT -j {OUTPUT_CHAIN}
done
ip6tables -w -I OUTPUT 1 -j {OUTPUT_CHAIN}
ip6tables -w -A {OUTPUT_CHAIN} -o lo -j ACCEPT
ip6tables -w -A {OUTPUT_CHAIN} -m conntrack \
  --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
ip6tables -w -A {OUTPUT_CHAIN} -j DROP
else
  # Some OSWorld images expose legacy ip6tables without an IPv6 filter
  # table. In that case fail closed unless the outer namespace has neither a
  # global IPv6 address nor a default IPv6 route. Guest IPv6 is independently
  # dropped below at the qemu bridge by ebtables.
  test -z "$(ip -6 -o addr show scope global)"
  test -z "$(ip -6 route show default)"
fi

ebtables -N {L2_CHAIN} 2>/dev/null || true
ebtables -F {L2_CHAIN}
while ebtables -D INPUT -i qemu -j {L2_CHAIN} 2>/dev/null; do
  :
done
while ebtables -D FORWARD -i qemu -j {L2_CHAIN} 2>/dev/null; do
  :
done
ebtables -I INPUT 1 -i qemu -j {L2_CHAIN}
ebtables -I FORWARD 1 -i qemu -j {L2_CHAIN}
ebtables -A {L2_CHAIN} -p IPv6 -j DROP
ebtables -A {L2_CHAIN} -j RETURN

iptables -w -t nat -C PREROUTING -i {GUEST_BRIDGE} -j {NAT_CHAIN}
iptables -w -C FORWARD -i {GUEST_BRIDGE} -j {FORWARD_CHAIN}
iptables -w -C {FORWARD_CHAIN} -m conntrack \
  --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
iptables -w -C {FORWARD_CHAIN} -j DROP
iptables -w -C INPUT -i {GUEST_BRIDGE} -j {INPUT_CHAIN}
iptables -w -C {INPUT_CHAIN} -p udp --sport 68 --dport 67 -j ACCEPT
iptables -w -C {INPUT_CHAIN} -j DROP
iptables -w -C OUTPUT -j {OUTPUT_CHAIN}
iptables -w -C {OUTPUT_CHAIN} -j DROP
if test "$e15_ipv6_mode" = ip6tables-plus-ebtables; then
  ip6tables -w -C FORWARD -i {GUEST_BRIDGE} -j {FORWARD_CHAIN}
  ip6tables -w -C {FORWARD_CHAIN} -j DROP
  ip6tables -w -C INPUT -i {GUEST_BRIDGE} -j {INPUT_CHAIN}
  ip6tables -w -C {INPUT_CHAIN} -j DROP
  ip6tables -w -C OUTPUT -j {OUTPUT_CHAIN}
  ip6tables -w -C {OUTPUT_CHAIN} -j DROP
else
  test -z "$(ip -6 -o addr show scope global)"
  test -z "$(ip -6 route show default)"
fi
ebtables-save | grep -Fqx -- '-A INPUT -i qemu -j {L2_CHAIN}'
ebtables-save | grep -Fqx -- '-A FORWARD -i qemu -j {L2_CHAIN}'
ebtables-save | grep -Fqx -- '-A {L2_CHAIN} -p IPv6 -j DROP'
{target_checks}
echo E15_EGRESS_IPV6_MODE=$e15_ipv6_mode
echo E15_EGRESS_SEAL_OK={mode}
"""


@dataclass(frozen=True)
class OuterFirewallReceipt:
    rules_sha256: str
    ipv6_enforcement: str


def install_outer_firewall_receipt(
        container: Any, *, mode: str, gateway_ip: str | None = None,
        ports: GatewayPorts | None = None) -> OuterFirewallReceipt:
    """Install and verify the seal inside the trusted outer Docker container."""

    script = _firewall_script(
        mode=mode, gateway_ip=gateway_ip, ports=ports)
    try:
        result = container.exec_run(
            ["sh", "-ceu", script], user="root", privileged=False)
    except Exception as exc:  # noqa: BLE001 - Docker SDK boundary
        raise E15EgressError(
            f"outer-container firewall install failed: {type(exc).__name__}") \
            from exc
    code, output = _exec_result(result)
    marker = f"E15_EGRESS_SEAL_OK={mode}"
    ipv6_markers = [
        line.split("=", 1)[1] for line in output.splitlines()
        if line.startswith("E15_EGRESS_IPV6_MODE=")]
    accepted_ipv6 = {
        "ip6tables-plus-ebtables",
        "ebtables-authoritative-no-outer-ipv6",
    }
    if (code != 0 or marker not in output or len(ipv6_markers) != 1
            or ipv6_markers[0] not in accepted_ipv6):
        raise E15EgressError(
            f"outer-container firewall verification failed (exit {code}): "
            f"{output[-1000:]}")
    return OuterFirewallReceipt(
        rules_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
        ipv6_enforcement=ipv6_markers[0])


def install_outer_firewall(
        container: Any, *, mode: str, gateway_ip: str | None = None,
        ports: GatewayPorts | None = None) -> str:
    """Compatibility wrapper returning the rules digest only."""

    return install_outer_firewall_receipt(
        container, mode=mode, gateway_ip=gateway_ip,
        ports=ports).rules_sha256


def _outer_container(environment: Any) -> Any:
    provider = getattr(environment, "provider", None)
    container = getattr(provider, "container", None)
    if container is None or not callable(getattr(container, "exec_run", None)):
        raise E15EgressError(
            "E15 V12 egress seal requires the live Docker provider container")
    try:
        container.reload()
    except Exception as exc:  # noqa: BLE001 - Docker SDK boundary
        raise E15EgressError("could not inspect the outer Docker container") from exc
    if getattr(container, "status", "") != "running":
        raise E15EgressError("outer Docker container is not running")
    return container


def _container_gateway_ip(container: Any) -> str:
    networks = (getattr(container, "attrs", {})
                .get("NetworkSettings", {}).get("Networks", {}))
    gateways = {
        value.get("Gateway") for value in networks.values()
        if isinstance(value, dict) and value.get("Gateway")
    }
    if len(gateways) != 1:
        raise E15EgressError(
            "outer Docker container does not have one unambiguous host gateway")
    gateway_ip = str(next(iter(gateways)))
    _validated_gateway_endpoint(gateway_ip, GatewayPorts(1, 1))
    return gateway_ip


@dataclass(frozen=True)
class SealInstallation:
    mode: str
    policy_sha256: str
    rules_sha256: str
    outer_container_id: str
    gateway_ip: str | None
    gateway_ports: GatewayPorts | None
    gateway_ca_sha256: str | None
    gateway_leaf_spki_sha256: str | None
    pinned_upstream_ips: tuple[str, ...]
    ipv6_enforcement: str


@dataclass(frozen=True)
class SealProbe:
    mode: str
    exact_streamview_https: str
    wrong_sni_cotenant: str
    direct_ip_https: str
    public_dns: str
    outer_gateway_service: str
    public_non_http_tcp: str
    public_ipv6: str
    transcript_sha256: str


class V12EgressSeal:
    """Own the trusted gateway and re-install outer rules after every reset."""

    def __init__(
            self, allowed_host: str = ALLOWED_STREAMVIEW_HOST,
            *, gateway_factory: Callable[..., FilteringTcpGateway] =
            FilteringTcpGateway) -> None:
        self.allowed_host = _canonical_dns_name(allowed_host)
        self._gateway_factory = gateway_factory
        self._gateway: FilteringTcpGateway | None = None
        self._gateway_ip: str | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._gateway is not None:
                self._gateway.close()
            self._gateway = None
            self._gateway_ip = None

    @property
    def chrome_spki_allowlist(self) -> str:
        """Base64 SPKI digest accepted by Chrome's narrow certificate flag."""

        with self._lock:
            if self._gateway is None:
                # Generate the same per-process gateway identity before the
            # first reset so trusted setup can launch Chrome with a narrow
                # certificate exception. Binding the listener still waits for
                # the live container's host-gateway address.
                gateway = self._gateway_factory(
                    "127.0.0.1", self.allowed_host)
                self._gateway = gateway
                self._gateway_ip = None
            return self._gateway.identity.leaf_spki_sha256_b64

    def _ensure_gateway(self, gateway_ip: str) -> FilteringTcpGateway:
        if self._gateway is not None and self._gateway_ip != gateway_ip:
            # ``chrome_spki_allowlist`` may have pre-created an unbound gateway
            # solely to expose its identity. Reuse that exact identity when
            # binding to Docker's host-gateway address.
            identity = self._gateway.identity
            upstream_ips = self._gateway.upstream_ips
            self._gateway.close()
            self._gateway = self._gateway_factory(
                gateway_ip, self.allowed_host, identity=identity,
                upstream_ips=upstream_ips)
            self._gateway_ip = gateway_ip
        if self._gateway is None:
            gateway = self._gateway_factory(gateway_ip, self.allowed_host)
            gateway.start()
            self._gateway = gateway
            self._gateway_ip = gateway_ip
        else:
            self._gateway.start()
        return self._gateway

    def _install_guest_target_identity(
            self, vm: Any, gateway_ip: str,
            gateway: FilteringTcpGateway) -> None:
        """Trust only this run's ephemeral relay CA and bind the exact hostname."""

        ca_b64 = base64.b64encode(gateway.identity.ca_pem).decode("ascii")
        environment = getattr(vm, "env", None)
        password = str(getattr(environment, "client_password", ""))
        if not password:
            raise E15EgressError("guest elevation password is unavailable")
        password_b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
        command = f"""
set -eu
pw=$(printf %s {password_b64} | base64 -d)
printf '%s\n' "$pw" | sudo -S -p '' sh -ceu '
  printf %s {ca_b64} | base64 -d > \
    /usr/local/share/ca-certificates/e15-v12-egress.crt
  chmod 0644 /usr/local/share/ca-certificates/e15-v12-egress.crt
  update-ca-certificates >/dev/null
  sed -i "/[[:space:]]# E15_V12_EGRESS$/d" /etc/hosts
  printf "%s\\t%s\\t# E15_V12_EGRESS\\n" \
    "{gateway_ip}" "{self.allowed_host}" >> /etc/hosts
'
unset pw
test "$(getent ahostsv4 '{self.allowed_host}' | awk 'NR==1 {{print $1}}')" = \
  '{gateway_ip}'
echo E15_GUEST_RELAY_IDENTITY_OK=1
"""
        output = vm.run_command(command, timeout=180, cap=4096) or ""
        if "E15_GUEST_RELAY_IDENTITY_OK=1" not in output:
            raise E15EgressError(
                "guest relay CA/hostname installation failed: " + output[-1000:])

    def install(self, vm: Any, *, mode: str) -> SealInstallation:
        """Seal the current post-reset outer container, failing closed."""

        environment = getattr(vm, "env", None)
        container = _outer_container(environment)
        container_id = str(getattr(container, "id", ""))
        if not container_id:
            raise E15EgressError("outer Docker container lacks an identity")
        with self._lock:
            if mode == "target":
                gateway_ip = _container_gateway_ip(container)
                gateway = self._ensure_gateway(gateway_ip)
                ports = gateway.ports
                self._install_guest_target_identity(vm, gateway_ip, gateway)
                firewall = install_outer_firewall_receipt(
                    container, mode=mode, gateway_ip=gateway_ip, ports=ports)
                ca_sha = hashlib.sha256(gateway.identity.ca_pem).hexdigest()
                spki_sha = gateway.identity.leaf_spki_sha256
                upstream_ips = gateway.upstream_ips
            elif mode == "null":
                gateway_ip = None
                ports = None
                ca_sha = None
                spki_sha = None
                upstream_ips = ()
                firewall = install_outer_firewall_receipt(
                    container, mode=mode)
            else:
                raise E15EgressError("egress mode must be target or null")
        return SealInstallation(
            mode=mode, policy_sha256=egress_policy_sha256(self.allowed_host),
            rules_sha256=firewall.rules_sha256,
            outer_container_id=container_id,
            gateway_ip=gateway_ip, gateway_ports=ports,
            gateway_ca_sha256=ca_sha,
            gateway_leaf_spki_sha256=spki_sha,
            pinned_upstream_ips=upstream_ips,
            ipv6_enforcement=firewall.ipv6_enforcement)

    def probe(
            self, vm: Any, installation: SealInstallation) -> SealProbe:
        """Exercise the live post-install boundary from inside the guest.

        This is a transport attestation, not an Agent task grade. Target mode
        requires the exact origin to work; null mode requires it to fail. Both
        modes require co-tenant SNI, direct-IP TLS, DNS, outer-container
        services, non-HTTP TCP, and IPv6 to fail.
        """

        if installation.mode not in {"target", "null"}:
            raise E15EgressError("cannot probe an unknown egress mode")
        pinned_ip = (installation.pinned_upstream_ips[0]
                     if installation.pinned_upstream_ips else "1.1.1.1")
        expected_exact = "PASS" if installation.mode == "target" else "BLOCKED"
        python_probe = r'''
import socket

def blocked(name, family, address, port, udp=False):
    sock = socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        if udp:
            # Minimal A query for example.com, transaction id 0xe150.
            query = bytes.fromhex(
                "e15001000001000000000000076578616d706c6503636f6d0000010001")
            sock.sendto(query, (address, port))
            sock.recvfrom(512)
        else:
            sock.connect((address, port))
    except OSError:
        print(f"E15_PROBE_{name}=BLOCKED")
    else:
        print(f"E15_PROBE_{name}=OPEN")
    finally:
        sock.close()

blocked("DNS", socket.AF_INET, "20.20.20.1", 53, udp=True)
blocked("OUTER_GATEWAY", socket.AF_INET, "20.20.20.1", 53)
blocked("NON_HTTP", socket.AF_INET, "1.1.1.1", 53)
blocked("IPV6", socket.AF_INET6, "2606:4700:4700::1111", 443)
'''
        encoded_probe = base64.b64encode(
            python_probe.encode("utf-8")).decode("ascii")
        command = f"""
set +e
if curl --noproxy '*' -fsS --connect-timeout 8 --max-time 25 \
  'https://{self.allowed_host}/' >/dev/null 2>&1; then
  echo E15_PROBE_EXACT=PASS
else
  echo E15_PROBE_EXACT=BLOCKED
fi
if curl --noproxy '*' -kfsS --connect-timeout 5 --max-time 10 \
  --resolve 'gitlab.web.hku.icu:443:{pinned_ip}' \
  'https://gitlab.web.hku.icu/' >/dev/null 2>&1; then
  echo E15_PROBE_WRONG_SNI=OPEN
else
  echo E15_PROBE_WRONG_SNI=BLOCKED
fi
if curl --noproxy '*' -kfsS --connect-timeout 5 --max-time 10 \
  'https://{pinned_ip}/' >/dev/null 2>&1; then
  echo E15_PROBE_DIRECT_IP=OPEN
else
  echo E15_PROBE_DIRECT_IP=BLOCKED
fi
printf %s {encoded_probe} | base64 -d | python3 -
echo E15_PROBE_DONE=1
"""
        output = vm.run_command(command, timeout=90, cap=16_384) or ""
        expected = {
            "EXACT": expected_exact,
            "WRONG_SNI": "BLOCKED",
            "DIRECT_IP": "BLOCKED",
            "DNS": "BLOCKED",
            "OUTER_GATEWAY": "BLOCKED",
            "NON_HTTP": "BLOCKED",
            "IPV6": "BLOCKED",
        }
        observed: dict[str, str] = {}
        for line in output.splitlines():
            if not line.startswith("E15_PROBE_") or "=" not in line:
                continue
            name, value = line.removeprefix("E15_PROBE_").split("=", 1)
            if name in observed:
                raise E15EgressError("egress probe emitted a duplicate marker")
            observed[name] = value
        if (observed.get("DONE") != "1"
                or any(observed.get(name) != value
                       for name, value in expected.items())):
            raise E15EgressError(
                "live guest egress probe failed closed: " + output[-2000:])
        return SealProbe(
            mode=installation.mode,
            exact_streamview_https=observed["EXACT"],
            wrong_sni_cotenant=observed["WRONG_SNI"],
            direct_ip_https=observed["DIRECT_IP"],
            public_dns=observed["DNS"],
            outer_gateway_service=observed["OUTER_GATEWAY"],
            public_non_http_tcp=observed["NON_HTTP"],
            public_ipv6=observed["IPV6"],
            transcript_sha256=hashlib.sha256(
                output.encode("utf-8")).hexdigest())
