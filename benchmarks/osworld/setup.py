"""Disposable OSWorld practice guests and network isolation."""
from __future__ import annotations
import logging
from typing import Any
log = logging.getLogger("rsiagent.practice")

def _boot_vm():
    from desktop_env.desktop_env import DesktopEnv

    from env.vm import VM

    class NoEvaluationDesktopEnv(DesktopEnv):
        def evaluate(self):  # pragma: no cover - the invariant is never call
            raise RuntimeError(
                "official evaluation is disabled inside TARGET evolution"
            )

    environment = NoEvaluationDesktopEnv(
        provider_name="docker",
        action_space="pyautogui",
        os_type="Ubuntu",
        screen_size=(1920, 1080),
        headless=True,
        require_a11y_tree=False,
        volume_size=60,
    )
    return environment, VM(environment)


def _egress_receipt(installation, probe) -> dict[str, Any]:
    """Return the security-relevant, JSON-safe seal receipt."""

    return {
        "schema_version": 1,
        "mode": installation.mode,
        "policy_sha256": installation.policy_sha256,
        "rules_sha256": installation.rules_sha256,
        "gateway_ca_sha256": installation.gateway_ca_sha256,
        "gateway_leaf_spki_sha256": installation.gateway_leaf_spki_sha256,
        "pinned_upstream_ips": list(installation.pinned_upstream_ips),
        "ipv6_enforcement": installation.ipv6_enforcement,
        "probe": {
            "mode": probe.mode,
            "exact_streamview_https": probe.exact_streamview_https,
            "wrong_sni_cotenant": probe.wrong_sni_cotenant,
            "direct_ip_https": probe.direct_ip_https,
            "public_dns": probe.public_dns,
            "outer_gateway_service": probe.outer_gateway_service,
            "public_non_http_tcp": probe.public_non_http_tcp,
            "public_ipv6": probe.public_ipv6,
            "transcript_sha256": probe.transcript_sha256,
        },
    }


def _reset_null_vm_sealed(
    vm, target_direction: str, egress_seal, prepare_unsealed=None
) -> dict:
    """Create a tool-complete null VM, then deny all guest public egress.

    ``prepare_unsealed`` is a trusted host-side application provisioner.  It
    runs after the ordinary null reset but before the egress seal, without any
    Agent context, target asset, evaluator, or grading surface.  This keeps a
    task-visible application available to Curriculum-authored practice while
    preserving the sealed learning boundary seen by every Agent.
    """

    from explore.practice_loop import _reset_null_vm

    result = _reset_null_vm(vm, target_direction)
    if not result.get("ok"):
        return result
    try:
        if prepare_unsealed is not None:
            if not callable(prepare_unsealed):
                raise RuntimeError("null tool provisioner is not callable")
            environment = getattr(vm, "env", None)
            setup_controller = getattr(environment, "setup_controller", None)
            if setup_controller is None:
                raise RuntimeError("null VM lacks a setup controller for trusted tools")
            prepare_unsealed(setup_controller)
        installation = egress_seal.install(vm, mode="null")
        probe = egress_seal.probe(vm, installation)
    except Exception as exc:  # noqa: BLE001 - fail closed at reset boundary
        return {"ok": False, "error": f"null pre-seal boundary failed: {exc}"}
    return {"ok": True, "egress": _egress_receipt(installation, probe)}


def _close(environment) -> None:
    close = getattr(environment, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - best-effort after durable state
            log.exception("environment close failed")
