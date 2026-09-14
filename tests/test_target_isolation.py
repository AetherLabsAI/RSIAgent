from __future__ import annotations

from dataclasses import dataclass

import pytest

from explore.target_isolation import (
    ALLOWED_STREAMVIEW_HOST,
    RSIEgressError,
    FORWARD_CHAIN,
    FilteringTcpGateway,
    INPUT_CHAIN,
    NAT_CHAIN,
    GatewayPorts,
    SealInstallation,
    TargetIsolationSeal,
    PrefaceIncomplete,
    PrefaceRejected,
    _firewall_script,
    egress_policy_manifest,
    egress_policy_sha256,
    install_outer_firewall,
    parse_tls_client_hello_sni,
    prepare_single_http_request,
    validate_http_preface,
    validate_tls_preface,
)


def _http(host: str, *, target: str = "/watch/example",
          extra: bytes = b"") -> bytes:
    return (f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
            "User-Agent: test\r\n\r\n").encode("ascii") + extra


def _client_hello(host: str | None, *, duplicate_sni: bool = False,
                  split_record: bool = False) -> bytes:
    extensions = bytearray()
    if host is not None:
        name = host.encode("ascii")
        entry = b"\x00" + len(name).to_bytes(2, "big") + name
        sni = len(entry).to_bytes(2, "big") + entry
        extension = b"\x00\x00" + len(sni).to_bytes(2, "big") + sni
        extensions.extend(extension)
        if duplicate_sni:
            extensions.extend(extension)
    body = (
        b"\x03\x03" + b"R" * 32
        + b"\x00"
        + b"\x00\x02\x13\x01"
        + b"\x01\x00"
        + len(extensions).to_bytes(2, "big") + bytes(extensions)
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body

    def record(payload: bytes) -> bytes:
        return b"\x16\x03\x01" + len(payload).to_bytes(2, "big") + payload

    if split_record:
        cut = 17
        return record(handshake[:cut]) + record(handshake[cut:])
    return record(handshake)


@pytest.mark.parametrize("host", [
    ALLOWED_STREAMVIEW_HOST,
    ALLOWED_STREAMVIEW_HOST.upper(),
    f"{ALLOWED_STREAMVIEW_HOST}:80",
])
def test_http_host_allowlist_accepts_only_exact_origin(host):
    assert validate_http_preface(_http(host)) == ALLOWED_STREAMVIEW_HOST


def test_https_inner_host_accepts_matching_443_authority():
    request = _http(
        f"{ALLOWED_STREAMVIEW_HOST}:443",
        target=f"https://{ALLOWED_STREAMVIEW_HOST}/watch/example")
    assert validate_http_preface(
        request, expected_port=443, expected_scheme="https") \
        == ALLOWED_STREAMVIEW_HOST


@pytest.mark.parametrize("host", [
    "evil.web.hku.icu",
    f"{ALLOWED_STREAMVIEW_HOST}.evil.invalid",
    f"evil.{ALLOWED_STREAMVIEW_HOST}",
    f"{ALLOWED_STREAMVIEW_HOST}.",
    f"{ALLOWED_STREAMVIEW_HOST}:81",
    f"user@{ALLOWED_STREAMVIEW_HOST}",
])
def test_http_host_allowlist_rejects_cotenants_suffixes_and_odd_authority(host):
    with pytest.raises(PrefaceRejected):
        validate_http_preface(_http(host))


def test_http_rejects_duplicate_host_connect_and_absolute_target_smuggling():
    duplicate = (
        f"GET / HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n"
        "Host: evil.web.hku.icu\r\n\r\n").encode()
    connect = (
        f"CONNECT {ALLOWED_STREAMVIEW_HOST}:443 HTTP/1.1\r\n"
        f"Host: {ALLOWED_STREAMVIEW_HOST}\r\n\r\n").encode()
    smuggled = _http(
        ALLOWED_STREAMVIEW_HOST,
        target="http://evil.web.hku.icu/private")
    for request in (duplicate, connect, smuggled):
        with pytest.raises(PrefaceRejected):
            validate_http_preface(request)


def test_http_requires_a_complete_header():
    with pytest.raises(PrefaceIncomplete):
        validate_http_preface(
            f"GET / HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n".encode())


def test_same_buffer_pipelined_cotenant_request_is_rejected():
    first = _http(ALLOWED_STREAMVIEW_HOST)
    second = _http("gitlab.web.hku.icu")
    with pytest.raises(PrefaceRejected, match="beyond its sole"):
        prepare_single_http_request(first + second)


@pytest.mark.parametrize("header", [
    "Transfer-Encoding: chunked",
    "Expect: 100-continue",
    "Upgrade: websocket",
    "TE: trailers",
])
def test_single_request_proxy_rejects_tunnel_and_ambiguous_framing(header):
    request = (
        f"POST /api/state HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n"
        f"{header}\r\n\r\n").encode()
    with pytest.raises(PrefaceRejected, match="forbidden framing"):
        prepare_single_http_request(request)
    duplicate = (
        f"POST / HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n"
        "Content-Length: 3\r\nContent-Length: 3\r\n\r\nabc").encode()
    with pytest.raises(PrefaceRejected, match="ambiguous Content-Length"):
        prepare_single_http_request(duplicate)


def test_single_request_proxy_forces_canonical_host_and_connection_close():
    request = (
        f"POST /api/state HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}:80\r\n"
        "Connection: keep-alive\r\nContent-Length: 3\r\n\r\nab").encode()
    prepared = prepare_single_http_request(request)
    assert f"Host: {ALLOWED_STREAMVIEW_HOST}\r\n".encode() in prepared.header
    assert b"Connection: close\r\n" in prepared.header
    assert b"keep-alive" not in prepared.header
    assert prepared.content_length == 3
    assert prepared.initial_body == b"ab"


class _DownstreamSocket:
    def __init__(self, reads):
        self.reads = list(reads)
        self.recv_calls = 0
        self.sent = bytearray()

    def recv(self, _size):
        self.recv_calls += 1
        return self.reads.pop(0) if self.reads else b""

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, _timeout):
        pass


class _UpstreamSocket:
    def __init__(self):
        self.sent = bytearray()
        self.responses = [
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
            b"Content-Length: 2\r\n\r\nOK",
            b"",
        ]

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, _size):
        return self.responses.pop(0)

    def settimeout(self, _timeout):
        pass

    def close(self):
        pass


def test_later_keepalive_request_is_never_read_or_relayed_upstream():
    upstream = _UpstreamSocket()
    gateway = FilteringTcpGateway(
        "127.0.0.1", connect=lambda *_args: upstream,
        upstream_ips=("127.0.0.1",))
    downstream = _DownstreamSocket([
        _http(ALLOWED_STREAMVIEW_HOST),
        _http("gitlab.web.hku.icu"),
    ])
    gateway._handle(downstream, "http")
    assert downstream.recv_calls == 1
    assert b"gitlab.web.hku.icu" not in upstream.sent
    assert upstream.sent.count(b"Host:") == 1
    assert b"Connection: close" in upstream.sent
    assert bytes(downstream.sent).endswith(b"OK")


@pytest.mark.parametrize("split_record", [False, True])
def test_tls_sni_allowlist_accepts_exact_sni_across_records(split_record):
    hello = _client_hello(ALLOWED_STREAMVIEW_HOST, split_record=split_record)
    assert parse_tls_client_hello_sni(hello) == ALLOWED_STREAMVIEW_HOST
    assert validate_tls_preface(hello) == ALLOWED_STREAMVIEW_HOST


@pytest.mark.parametrize("host", [
    "evil.web.hku.icu",
    f"{ALLOWED_STREAMVIEW_HOST}.evil.invalid",
    f"{ALLOWED_STREAMVIEW_HOST}.",
])
def test_tls_sni_allowlist_rejects_wrong_or_noncanonical_sni(host):
    with pytest.raises(PrefaceRejected):
        validate_tls_preface(_client_hello(host))


def test_tls_requires_exactly_one_sni_hostname():
    for hello in (
            _client_hello(None),
            _client_hello(ALLOWED_STREAMVIEW_HOST, duplicate_sni=True)):
        with pytest.raises(PrefaceRejected):
            validate_tls_preface(hello)


def test_target_firewall_redirects_only_http_https_then_drops_everything_else():
    ports = GatewayPorts(http=19080, https=19443)
    script = _firewall_script(
        mode="target", gateway_ip="172.17.0.1", ports=ports)
    assert f"-i dockerbridge -j {NAT_CHAIN}" in script
    assert "--dport 80 -j DNAT --to-destination 172.17.0.1:19080" in script
    assert "--dport 443 -j DNAT --to-destination 172.17.0.1:19443" in script
    assert f"-i dockerbridge -j {FORWARD_CHAIN}" in script
    assert f"-i dockerbridge -j {INPUT_CHAIN}" in script
    assert "--ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT" in \
        script.replace("\\\n", "")
    assert "ip6tables" in script
    assert "ebtables -A RSI_TARGET_L2 -p IPv6 -j DROP" in script
    assert "qemu-system-x86_64" in script
    assert "/sys/class/net/qemu/master" in script
    assert "iptables -w -I OUTPUT 1 -j RSI_TARGET_OUT" in script
    assert "--sport 68 --dport 67 -j ACCEPT" in script
    assert "--dport 53" not in script
    assert script.index(f"-A {FORWARD_CHAIN} -m conntrack") \
        < script.index(f"-A {FORWARD_CHAIN} -j DROP")
    # Match every packet arriving from the QEMU bridge, not the spoofable
    # default guest source address.
    assert "-s 20.20.20.21" not in script


def test_null_firewall_has_no_gateway_allow_and_still_installs_drop_chains():
    script = _firewall_script(mode="null")
    assert "--to-destination" not in script
    assert f"-A {FORWARD_CHAIN} -j DROP" in script
    assert f"-A {INPUT_CHAIN} -j DROP" in script
    assert f"-A RSI_TARGET_OUT -j DROP" in script
    assert "RSI_EGRESS_SEAL_OK=null" in script


@dataclass
class _ExecResult:
    exit_code: int
    output: bytes


class _Container:
    def __init__(self, *, exit_code: int = 0, marker: bool = True,
                 ipv6_mode: str = "ip6tables-plus-ebtables"):
        self.calls = []
        self.exit_code = exit_code
        self.marker = marker
        self.ipv6_mode = ipv6_mode

    def exec_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        mode = "target" if "RSI_EGRESS_SEAL_OK=target" in argv[-1] else "null"
        output = (
            f"RSI_EGRESS_IPV6_MODE={self.ipv6_mode}\n"
            f"RSI_EGRESS_SEAL_OK={mode}\n").encode() if self.marker else b""
        return _ExecResult(self.exit_code, output)


def test_outer_firewall_install_uses_root_exec_and_binds_script_hash():
    container = _Container()
    digest = install_outer_firewall(
        container, mode="target", gateway_ip="172.17.0.1",
        ports=GatewayPorts(19080, 19443))
    assert len(digest) == 64
    argv, kwargs = container.calls[0]
    assert argv[:2] == ["sh", "-ceu"]
    assert kwargs == {"user": "root", "privileged": False}
    assert "iptables -w -C" in argv[-1]


def test_outer_firewall_install_fails_closed_on_exec_or_marker_failure():
    with pytest.raises(RSIEgressError):
        install_outer_firewall(_Container(exit_code=1), mode="null")
    with pytest.raises(RSIEgressError):
        install_outer_firewall(_Container(marker=False), mode="null")
    with pytest.raises(RSIEgressError):
        install_outer_firewall(
            _Container(ipv6_mode="unexpected-ip6-failure"), mode="null")


def test_real_image_missing_ip6_filter_has_narrow_ebtables_fallback():
    script = _firewall_script(mode="null")
    assert "practice_ip6_rc\" -eq 3" in script
    assert "Table does not exist" in script
    assert "ebtables-authoritative-no-outer-ipv6" in script
    assert 'ip -6 -o addr show scope global' in script
    assert 'ip -6 route show default' in script
    assert "exit 73" in script


def test_policy_provenance_declares_double_https_gate_and_null_deny_all():
    policy = egress_policy_manifest()
    target = policy["target_public_egress"]
    assert target["host"] == ALLOWED_STREAMVIEW_HOST
    assert "https-terminated-sni-exact-and-inner-host-exact" \
        in target["protocols"]
    assert policy["null_project_public_egress"] == []
    assert "guest-dns" in policy["blocked"]
    assert len(egress_policy_sha256()) == 64


class _ProbeVM:
    def __init__(self, output):
        self.output = output
        self.commands = []

    def run_command(self, command, **_kwargs):
        self.commands.append(command)
        return self.output


def _installation(mode="target"):
    return SealInstallation(
        mode=mode, policy_sha256="a" * 64, rules_sha256="b" * 64,
        outer_container_id="container", gateway_ip="172.17.0.1",
        gateway_ports=GatewayPorts(19080, 19443),
        gateway_ca_sha256="c" * 64,
        gateway_leaf_spki_sha256="d" * 64,
        pinned_upstream_ips=("18.209.93.223",) if mode == "target" else (),
        ipv6_enforcement="ip6tables-plus-ebtables")


def test_live_probe_seam_requires_positive_exact_and_all_negative_markers():
    output = "\n".join([
        "RSI_PROBE_EXACT=PASS",
        "RSI_PROBE_WRONG_SNI=BLOCKED",
        "RSI_PROBE_DIRECT_IP=BLOCKED",
        "RSI_PROBE_DNS=BLOCKED",
        "RSI_PROBE_OUTER_GATEWAY=BLOCKED",
        "RSI_PROBE_NON_HTTP=BLOCKED",
        "RSI_PROBE_IPV6=BLOCKED",
        "RSI_PROBE_DONE=1",
    ])
    seal = object.__new__(TargetIsolationSeal)
    seal.allowed_host = ALLOWED_STREAMVIEW_HOST
    vm = _ProbeVM(output)
    probe = seal.probe(vm, _installation())
    assert probe.exact_streamview_https == "PASS"
    assert probe.wrong_sni_cotenant == "BLOCKED"
    assert "gitlab.web.hku.icu" in vm.commands[0]


def test_live_probe_seam_fails_on_any_open_bypass():
    output = "\n".join([
        "RSI_PROBE_EXACT=PASS",
        "RSI_PROBE_WRONG_SNI=OPEN",
        "RSI_PROBE_DIRECT_IP=BLOCKED",
        "RSI_PROBE_DNS=BLOCKED",
        "RSI_PROBE_OUTER_GATEWAY=BLOCKED",
        "RSI_PROBE_NON_HTTP=BLOCKED",
        "RSI_PROBE_IPV6=BLOCKED",
        "RSI_PROBE_DONE=1",
    ])
    seal = object.__new__(TargetIsolationSeal)
    seal.allowed_host = ALLOWED_STREAMVIEW_HOST
    with pytest.raises(RSIEgressError):
        seal.probe(_ProbeVM(output), _installation())


class _FakeIdentity:
    ca_pem = b"ca"
    leaf_spki_sha256 = "e" * 64
    leaf_spki_sha256_b64 = "spki-b64"


class _FakeGateway:
    def __init__(self, bind_ip, _host, *, identity=None, upstream_ips=None):
        self.bind_ip = bind_ip
        self.identity = identity or _FakeIdentity()
        self.upstream_ips = upstream_ips or ("18.209.93.223",)
        self.started = False
        self.ports = GatewayPorts(19080, 19443)

    def start(self):
        self.started = True
        return self.ports

    def close(self):
        self.started = False


def test_precreated_chrome_identity_is_reused_and_started_after_real_rebind():
    seal = TargetIsolationSeal(gateway_factory=_FakeGateway)
    assert seal.chrome_spki_allowlist == "spki-b64"
    precreated_identity = seal._gateway.identity
    gateway = seal._ensure_gateway("172.17.0.1")
    assert gateway.bind_ip == "172.17.0.1"
    assert gateway.identity is precreated_identity
    assert gateway.started is True
