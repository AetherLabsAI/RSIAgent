#!/usr/bin/env python3
"""Launch EXP-2026-015 V12 task-visible self-evolution from empty memory.

V12 first gives a fresh Actor the normal solver-visible target setup, with every
evaluation callback stripped. A fresh Verifier judges that attempt, the same
Actor learns, and only then is the persistent Curriculum initialized. Practice
projects use null-task machines. Curriculum may request a fresh immutable target
test but cannot certify convergence itself. No official evaluator or benchmark
score is called here.

Typical launch::

    python run_e15.py --root e15_t056_scratch --scratch-lineage \
      --target-task-file results/operator_inputs/osworld_v2_t056_instruction.txt \
      --task-visible-ack I-ATTEST-TARGET-USES-NORMAL-TASK-VISIBLE-SETUP-NO-EVALUATOR-SCORE-OR-HIDDEN-GRADING
    python run_e15.py --root e15_t056_scratch --fence-probe ...
    python run_e15.py --root e15_t056_scratch --evolve \
      --execute LAUNCH-E15-T056-SCRATCH ...
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import signal
import shlex
import sys
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from config.runtime_paths import resolve_osworld_root

FORGE = Path(__file__).resolve().parent
OSWORLD = resolve_osworld_root(forge_root=FORGE)
RESULTS = FORGE / "results" / "explore"
CORPUS = RESULTS / "corpus_shingles.json"
EXECUTE_ACK = "LAUNCH-E15-T056-SCRATCH"
EXECUTE_ACKS = {
    "task_056": EXECUTE_ACK,
    "task_094": "LAUNCH-E15-T094-SCRATCH",
}
DETACHED_SETUP_TASKS = frozenset((
    "task_003", "task_019", "task_042", "task_044", "task_056",
    "task_063", "task_080", "task_084", "task_089", "task_094",
))
EXPERIMENT = "EXP-2026-015"
SETUP_PROJECTION_VERSION = (
    "detached-task056-local-assets-same-origin-tutorial-no-evaluate-v3")
TASK094_SETUP_PROJECTION_VERSION = (
    "detached-task094-local-reference-no-evaluate-v1")
TASK003_SETUP_PROJECTION_VERSION = (
    "detached-task003-local-assets-no-evaluate-v1")
DESKTOP_SETUP_PROJECTION_VERSION = (
    "detached-reviewed-desktop-local-assets-no-evaluate-v1")
STREAMVIEW_STATE_TRANSFORM = "target-video-same-origin-upload-v1"
TASK056_TUTORIAL_ID = "shotcut-cinematic-titles-056"

CONFIG_PATHS = {
    "actor": FORGE / "config/e15_actor.yaml",
    "verifier": FORGE / "config/e15_verify.yaml",
    "curriculum": FORGE / "config/e15_curriculum.yaml",
    "memory": FORGE / "config/e15_memory.yaml",
}

RUNTIME_FILES = (
    "run_e15.py",
    "explore/e15_loop.py",
    "explore/e15_egress.py",
    "explore/e15_v12_loop.py",
    "explore/e15_state.py",
    "explore/charter.py",
    "explore/commit.py",
    "explore/initialization.py",
    "explore/provision7.py",
    "explore/targeting.py",
    "core/actor.py",
    "core/checks.py",
    "core/eyes.py",
    "core/imagery.py",
    "core/loop.py",
    "core/trace.py",
    "core/render.py",
    "core/verifier.py",
    "env/vm.py",
    "llm/client.py",
    "config/settings.py",
    "tools/exam_fence.py",
)

log = logging.getLogger("forge.e15")


def _sha256_regular_local_file(path: Path) -> str:
    """Hash one immutable local setup input; remote/symlink inputs are invalid."""

    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Task056 setup input is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_task_asset(relative_path: str, *, task_label: str) -> str:
    """Resolve one task asset through OSWorld's configured local mirror."""

    from desktop_env.file_source import asset, resolve_local_source

    resolved = resolve_local_source(asset(relative_path))
    if resolved is None:
        raise RuntimeError(
            f"{task_label} asset did not resolve to the local mirror: "
            f"{relative_path}")
    path = Path(resolved)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(
            f"{task_label} local asset is not a regular file: {relative_path}")
    return str(path.resolve())


def _local_task056_asset(relative_path: str) -> str:
    return _local_task_asset(relative_path, task_label="Task056")


def _prepare_localized_streamview_state(
        *, streamview_base: str, state_path: Path, tutorial_path: Path,
        tutorial_id: str) -> tuple[str, str]:
    """Upload the hash-bound tutorial and rewrite only its state URL.

    The trusted host performs this setup operation. The desktop guest later
    reaches only the exact StreamView origin through the egress seal, so the
    tutorial never causes a public Hugging Face fetch from Agent context.
    """

    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    parsed_base = urlparse(streamview_base)
    if (parsed_base.scheme not in {"http", "https"}
            or parsed_base.hostname != "streamview.web.hku.icu"
            or parsed_base.username is not None
            or parsed_base.password is not None
            or parsed_base.path not in {"", "/"}
            or parsed_base.params or parsed_base.query or parsed_base.fragment):
        raise RuntimeError("StreamView setup origin is not the exact allowed host")
    if state_path.is_symlink() or not state_path.is_file():
        raise RuntimeError("StreamView state is not a regular local file")
    if tutorial_path.is_symlink() or not tutorial_path.is_file():
        raise RuntimeError("StreamView tutorial is not a regular local file")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("StreamView state is not valid local UTF-8 JSON") from exc
    if not isinstance(state, dict) or not isinstance(state.get("data"), dict):
        raise RuntimeError("StreamView state does not have the expected data object")
    videos = state["data"].get("videos")
    matches = [video for video in videos or []
               if isinstance(video, dict)
               and video.get("videoId") == tutorial_id]
    if not isinstance(videos, list) or len(matches) != 1:
        raise RuntimeError("StreamView state lacks one exact Task056 tutorial")

    origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
    state_endpoint = urljoin(origin, "/api/state")
    files_endpoint = urljoin(origin, "/api/files")
    session = requests.Session()
    initial = session.get(state_endpoint, timeout=30, verify=False)
    initial.raise_for_status()
    user_id = session.cookies.get("user_id") or initial.cookies.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise RuntimeError("StreamView did not issue a user_id cookie")

    with tutorial_path.open("rb") as tutorial:
        upload = session.post(
            files_endpoint,
            files=[("files", (tutorial_path.name, tutorial, "video/mp4"))],
            timeout=180, verify=False)
    upload.raise_for_status()
    try:
        upload_payload = upload.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError("StreamView upload response was not JSON") from exc
    uploaded = upload_payload.get("files") if isinstance(
        upload_payload, dict) else None
    if not isinstance(uploaded, list) or len(uploaded) != 1 \
            or not isinstance(uploaded[0], dict):
        raise RuntimeError("StreamView did not return exactly one uploaded file")
    uploaded_url = uploaded[0].get("url")
    if not isinstance(uploaded_url, str) or not uploaded_url:
        raise RuntimeError("StreamView upload omitted its same-origin URL")
    localized_url = urljoin(origin + "/", uploaded_url)
    parsed_localized = urlparse(localized_url)
    if (parsed_localized.scheme != parsed_base.scheme
            or parsed_localized.netloc != parsed_base.netloc
            or parsed_localized.username is not None
            or parsed_localized.password is not None
            or not parsed_localized.path.startswith("/api/files/")
            or parsed_localized.query or parsed_localized.fragment):
        raise RuntimeError("StreamView upload URL escaped the exact origin")

    # Treat the state update as bound only after the application serves back
    # the exact local bytes for this same user. A successful upload status or
    # plausible URL alone is not sufficient task-visible tutorial evidence.
    readback = session.get(
        localized_url, timeout=180, verify=False, stream=True,
        allow_redirects=False)
    if readback.status_code != 200:
        readback.close()
        raise RuntimeError(
            f"StreamView tutorial readback returned {readback.status_code}")
    readback_url = urlparse(str(readback.url))
    if (readback_url.scheme != parsed_base.scheme
            or readback_url.netloc != parsed_base.netloc
            or readback_url.path != parsed_localized.path
            or readback_url.query or readback_url.fragment):
        readback.close()
        raise RuntimeError("StreamView tutorial readback escaped its exact URL")
    readback_digest = hashlib.sha256()
    readback_bytes = 0
    try:
        for chunk in readback.iter_content(chunk_size=1024 * 1024):
            if chunk:
                readback_digest.update(chunk)
                readback_bytes += len(chunk)
    finally:
        readback.close()
    if (readback_bytes != tutorial_path.stat().st_size
            or readback_digest.hexdigest()
            != _sha256_regular_local_file(tutorial_path)):
        raise RuntimeError("StreamView tutorial readback bytes drifted")

    matches[0]["videoUrl"] = localized_url
    replaced = session.put(
        state_endpoint, json=state, timeout=60, verify=False)
    replaced.raise_for_status()
    return user_id, localized_url


class _Task003SetupOnly:
    """Detached Task003 setup and generic tool provisioner, without evaluator."""

    __slots__ = (
        "asset_paths",
        "chrome_spki_allowlist",
        "config",
        "disable_recording",
        "disable_vnc",
        "evaluator",
        "id",
        "instruction",
        "proxy",
        "user_simulator",
    )

    def __init__(
            self, *, instruction: str, proxy: bool,
            disable_vnc: bool, disable_recording: bool,
            asset_paths: tuple[str, str, str]) -> None:
        self.id = "003"
        self.instruction = instruction
        self.proxy = proxy
        self.config: list[dict[str, Any]] = []
        self.user_simulator = None
        self.evaluator = None
        self.disable_vnc = disable_vnc
        self.disable_recording = disable_recording
        self.asset_paths = asset_paths
        self.chrome_spki_allowlist = ""

    @property
    def setup_projection(self) -> str:
        return TASK003_SETUP_PROJECTION_VERSION

    @property
    def target_inputs(self) -> tuple[str, ...]:
        return (
            "/home/user/Desktop/city.zip",
            "/home/user/Desktop/filter.zip",
            "/home/user/Desktop/weather_of_hongkong.pptx",
        )

    @property
    def candidate_outputs(self) -> tuple[str, ...]:
        # Composite filenames encode Agent-selected source numbers, so the only
        # statically named output is the edited presentation. The unified target
        # runner evaluates the live Desktop and does not use V12 candidate capture.
        return ("/home/user/Desktop/weather_of_hongkong.pptx",)

    @property
    def setup_manifest(self) -> dict[str, Any]:
        names = ("city.zip", "filter.zip", "weather_hongkong.pptx")
        return {
            "schema_version": 1,
            "projection": self.setup_projection,
            "task_id": "task_003",
            "local_asset_sha256": {
                name: _sha256_regular_local_file(Path(path))
                for name, path in zip(names, self.asset_paths, strict=True)
            },
            "proxy": self.proxy,
            "disable_vnc": self.disable_vnc,
            "disable_recording": self.disable_recording,
            "trusted_preseal_packages": ["gimp", "libreoffice-impress"],
        }

    def prepare_unsealed(self, setup_controller) -> None:
        """Provision only generic authoring tools before public egress closes."""

        setup_controller.execute(
            command=(
                "set -eu; "
                "if ! command -v gimp >/dev/null 2>&1 || "
                "! command -v libreoffice >/dev/null 2>&1; then "
                "  echo '{CLIENT_PASSWORD}' | sudo -S apt-get update -qq "
                "2>&1 | tail -3; "
                "  echo '{CLIENT_PASSWORD}' | sudo -S apt-get install -y "
                "--no-install-recommends gimp libreoffice-impress "
                "2>&1 | tail -5; "
                "fi; command -v gimp; command -v libreoffice; "
                "echo E15_T003_TOOLS_READY=1"),
            shell=True, timeout=900)

    def setup(self, setup_controller, use_proxy: bool = False) -> None:
        """Apply ordinary solver-visible Task003 setup from detached local bytes."""

        del use_proxy
        desktop = "/home/user/Desktop"
        setup_controller.execute(["mkdir", "-p", desktop])
        city, filters, deck = self.asset_paths
        setup_controller.download([
            {"url": city, "path": f"{desktop}/city.zip"},
            {"url": filters, "path": f"{desktop}/filter.zip"},
            {"url": deck, "path": f"{desktop}/weather_of_hongkong.pptx"},
        ])
        setup_controller.launch(["gimp"])
        setup_controller.launch([
            "libreoffice", "--impress",
            f"{desktop}/weather_of_hongkong.pptx",
        ])


class _Task056SetupOnly:
    """Detached, data-only Task056 setup surface with no evaluator owner.

    This deliberately does not inherit from ``BaseTask`` and has no
    ``evaluate`` attribute. Its bound ``setup`` method therefore cannot lead
    back to the official Task056 object through ``setup.__self__``.
    """

    __slots__ = (
        "id", "instruction", "proxy", "config", "user_simulator",
        "evaluator", "disable_vnc", "disable_recording", "asset_urls",
        "streamview_state", "tutorial_asset", "tutorial_id",
        "chrome_spki_allowlist",
    )

    def __init__(
            self, *, instruction: str, proxy: bool,
            disable_vnc: bool, disable_recording: bool,
            asset_urls: tuple[str, str, str, str, str],
            streamview_state: str, tutorial_asset: str,
            tutorial_id: str) -> None:
        self.id = "056"
        self.instruction = instruction
        self.proxy = proxy
        self.config: list[dict[str, Any]] = []
        self.user_simulator = None
        self.evaluator = None
        self.disable_vnc = disable_vnc
        self.disable_recording = disable_recording
        self.asset_urls = asset_urls
        self.streamview_state = streamview_state
        self.tutorial_asset = tutorial_asset
        self.tutorial_id = tutorial_id
        self.chrome_spki_allowlist = ""

    @property
    def setup_projection(self) -> str:
        return SETUP_PROJECTION_VERSION

    @property
    def target_inputs(self) -> tuple[str, ...]:
        return (
            "/home/user/Desktop/raw_materials/2325093-hd.mp4",
            "/home/user/Desktop/raw_materials/4370831-hd.mp4",
            "/home/user/Desktop/raw_materials/1284284-hd.mp4",
            "/home/user/Desktop/raw_materials/BGM.mp3",
            "/home/user/Desktop/groundtruth_video.mp4",
        )

    @property
    def candidate_outputs(self) -> tuple[str, ...]:
        return (
            "/home/user/Desktop/OSWorld.mp4",
            "/home/user/Desktop/OSWorld/",
        )

    @property
    def setup_manifest(self) -> dict[str, Any]:
        bound_files = {
            "clip_1.mp4": self.asset_urls[0],
            "clip_2.mp4": self.asset_urls[1],
            "clip_3.mp4": self.asset_urls[2],
            "BGM.mp3": self.asset_urls[3],
            "OSWorld.mp4": self.asset_urls[4],
            "state-streamview.json": self.streamview_state,
            "shotcut_cinematic_titles_tutorial.mp4": self.tutorial_asset,
        }
        return {
            "schema_version": 2,
            "projection": self.setup_projection,
            "task_id": "task_056",
            "local_asset_sha256": {
                name: _sha256_regular_local_file(Path(path))
                for name, path in bound_files.items()
            },
            "tutorial_id": self.tutorial_id,
            "streamview_state_transform": STREAMVIEW_STATE_TRANSFORM,
            "proxy": self.proxy,
            "disable_vnc": self.disable_vnc,
            "disable_recording": self.disable_recording,
        }

    def setup(self, setup_controller, use_proxy: bool = False) -> None:
        """Apply the normal Task056 solver-visible setup from detached data."""

        from desktop_env.controllers.website import (
            build_website_url,
        )
        from desktop_env.safety import (
            setup_disk_monitor, setup_snap_confinement_baseline,
        )
        from playwright.sync_api import sync_playwright

        del use_proxy  # Task056's normal custom setup does not consume it.
        clip1, clip2, clip3, bgm, reference = self.asset_urls
        raw_materials = "/home/user/Desktop/raw_materials"
        desktop = "/home/user/Desktop"
        setup_controller.execute(["mkdir", "-p", raw_materials])
        setup_controller.download([
            {"url": clip1, "path": f"{raw_materials}/2325093-hd.mp4"},
            {"url": clip2, "path": f"{raw_materials}/4370831-hd.mp4"},
            {"url": clip3, "path": f"{raw_materials}/1284284-hd.mp4"},
            {"url": bgm, "path": f"{raw_materials}/BGM.mp3"},
        ])
        setup_controller.download([
            {"url": reference, "path": f"{desktop}/groundtruth_video.mp4"},
        ])
        chrome_command = ["google-chrome", "--remote-debugging-port=1337"]
        if self.chrome_spki_allowlist:
            chrome_command.append(
                "--ignore-certificate-errors-spki-list="
                + self.chrome_spki_allowlist)
        setup_controller.launch(chrome_command)
        setup_controller.launch([
            "socat", "tcp-listen:9222,fork", "tcp:localhost:1337"])

        streamview_base = build_website_url("streamview")
        user_id_cookie, localized_video_url = \
            _prepare_localized_streamview_state(
                streamview_base=streamview_base,
                state_path=Path(self.streamview_state),
                tutorial_path=Path(self.tutorial_asset),
                tutorial_id=self.tutorial_id)
        watch_url = f"{streamview_base}/watch/{self.tutorial_id}"
        remote_debugging_url = (
            f"http://{setup_controller.vm_ip}:"
            f"{setup_controller.chromium_port}")
        media_ready = False
        for attempt in range(15):
            if attempt:
                time.sleep(5)
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.connect_over_cdp(
                        remote_debugging_url)
                except Exception:
                    if attempt < 14:
                        continue
                    raise
                context = browser.contexts[0]
                context.add_cookies([{
                    "name": "user_id", "value": user_id_cookie,
                    "url": streamview_base,
                }])
                existing_real_pages = [
                    page for page in context.pages
                    if page.url and page.url not in {
                        "about:blank", "chrome://newtab/",
                        "chrome://new-tab-page/", ""}]
                blank_tabs = [
                    page for page in context.pages
                    if not page.url or page.url in {
                        "about:blank", "chrome://newtab/",
                        "chrome://new-tab-page/", ""}]
                page = context.new_page()
                try:
                    response = page.goto(
                        watch_url, wait_until="domcontentloaded",
                        timeout=60000)
                    if response is None or not 200 <= response.status < 400:
                        raise RuntimeError(
                            "Task056 StreamView navigation returned no "
                            "successful response")
                    page.wait_for_function(
                        """() => {
                          const video = document.querySelector('video');
                          return Boolean(video && video.currentSrc &&
                            video.readyState >= 1 &&
                            Number.isFinite(video.duration) &&
                            video.duration > 0 && !video.error);
                        }""",
                        timeout=60000)
                    media = page.eval_on_selector(
                        "video",
                        """video => ({
                          currentSrc: video.currentSrc,
                          readyState: video.readyState,
                          duration: video.duration,
                          error: video.error ? video.error.code : null
                        })""")
                    if (not isinstance(media, dict)
                            or media.get("currentSrc") != localized_video_url
                            or int(media.get("readyState", 0)) < 1
                            or float(media.get("duration", 0)) <= 0
                            or media.get("error") is not None
                            or urlparse(page.url).hostname
                            != urlparse(streamview_base).hostname):
                        raise RuntimeError(
                            "Task056 StreamView did not load the exact "
                            "localized tutorial")
                except Exception as exc:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001 - browser cleanup
                        pass
                    if attempt < 14:
                        log.warning(
                            "Task056 StreamView media not ready on attempt "
                            "%s: %s", attempt + 1, exc)
                        continue
                    raise
                if not existing_real_pages:
                    for blank_tab in blank_tabs:
                        try:
                            blank_tab.close()
                        except Exception:  # noqa: BLE001 - browser cleanup
                            pass
                media_ready = True
                break

        if not media_ready:
            raise RuntimeError("Task056 StreamView media readiness was not proven")

        # The target tutorial must be the same hash-bound local bytes uploaded
        # to this per-run StreamView user, never a public HuggingFace fetch.
        if urlparse(localized_video_url).hostname != urlparse(
                streamview_base).hostname:
            raise RuntimeError("localized tutorial escaped the StreamView origin")

        setup_controller._sleep_setup(1)
        setup_snap_confinement_baseline(
            setup_controller, task_id=self.id)
        setup_disk_monitor(setup_controller, task_id=self.id)


class _Task094SetupOnly:
    """Detached, data-only Task094 setup surface with no evaluator owner."""

    __slots__ = (
        "id", "instruction", "proxy", "config", "user_simulator",
        "evaluator", "disable_vnc", "disable_recording", "reference_asset",
        "chrome_spki_allowlist",
    )

    OUTPUT_DIR = "/home/user/Documents/SolveSpace"
    OUTPUT_FILE = f"{OUTPUT_DIR}/part.slvs"
    VIDEO_DIR = "/home/user/Videos"
    VIDEO_FILE = f"{VIDEO_DIR}/task094_ref.mp4"
    EVAL_DIR = "/tmp/task094_eval"
    START_TIMESTAMP = "/tmp/task_094_start_ts"

    def __init__(
            self, *, instruction: str, proxy: bool,
            disable_vnc: bool, disable_recording: bool,
            reference_asset: str) -> None:
        self.id = "094"
        self.instruction = instruction
        self.proxy = proxy
        self.config: list[dict[str, Any]] = []
        self.user_simulator = None
        self.evaluator = None
        self.disable_vnc = disable_vnc
        self.disable_recording = disable_recording
        self.reference_asset = reference_asset
        # The common reset boundary installs one exact-origin seal. Task094
        # launches no browser, so this value is deliberately unused here.
        self.chrome_spki_allowlist = ""

    @property
    def setup_projection(self) -> str:
        return TASK094_SETUP_PROJECTION_VERSION

    @property
    def target_inputs(self) -> tuple[str, ...]:
        return (self.VIDEO_FILE,)

    @property
    def candidate_outputs(self) -> tuple[str, ...]:
        return (self.OUTPUT_FILE,)

    @property
    def setup_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "projection": self.setup_projection,
            "task_id": "task_094",
            "local_asset_sha256": {
                "ref-video.mp4": _sha256_regular_local_file(
                    Path(self.reference_asset)),
            },
            "proxy": self.proxy,
            "disable_vnc": self.disable_vnc,
            "disable_recording": self.disable_recording,
            "trusted_preseal_packages": ["solvespace", "mpv"],
        }

    def prepare_unsealed(self, setup_controller) -> None:
        """Install generic GUI tools before the guest's public egress closes.

        This trusted host phase has no Agent context and no task/evaluator
        object. The ordinary Task094 setup below still performs its own exact
        presence check, but never needs package-network access after sealing.
        """

        setup_controller.execute(
            command=(
                "set -eu; "
                "if ! which solvespace-cli >/dev/null 2>&1 || "
                "! which mpv >/dev/null 2>&1; then "
                "  echo '{CLIENT_PASSWORD}' | sudo -S apt-get update -qq "
                "2>&1 | tail -3; "
                "  echo '{CLIENT_PASSWORD}' | sudo -S apt-get install -y "
                "--no-install-recommends solvespace mpv 2>&1 | tail -5; "
                "fi; "
                "test -x /usr/bin/solvespace-cli; test -x /usr/bin/mpv; "
                "echo E15_T094_TOOLS_READY=1"),
            shell=True, timeout=600)

    def setup(self, setup_controller, use_proxy: bool = False) -> None:
        """Apply the normal Task094 solver-visible setup from detached data."""

        del use_proxy
        setup_controller.execute(
            command=(
                "set -eu; "
                "if ! which solvespace-cli >/dev/null 2>&1 || "
                "! which mpv >/dev/null 2>&1; then "
                "  echo '{CLIENT_PASSWORD}' | sudo -S apt-get update -qq "
                "2>&1 | tail -3; "
                "  echo '{CLIENT_PASSWORD}' | sudo -S apt-get install -y "
                "--no-install-recommends solvespace mpv 2>&1 | tail -5; "
                "fi; "
                "which solvespace-cli || echo 'SOLVESPACE_CLI_NOT_FOUND'; "
                "which mpv || echo 'MPV_NOT_FOUND'"),
            shell=True, timeout=600)
        setup_controller.execute(
            command=(
                f"mkdir -p {self.OUTPUT_DIR} {self.VIDEO_DIR} "
                f"{self.EVAL_DIR} && "
                f"rm -f {self.OUTPUT_FILE} {self.EVAL_DIR}/*"),
            shell=True, timeout=10)
        setup_controller.download([
            {"url": self.reference_asset, "path": self.VIDEO_FILE},
        ])
        setup_controller.execute(
            command=f"date +%s > {self.START_TIMESTAMP}",
            shell=True, timeout=5)
        setup_controller.execute(
            command=(
                "pkill -f solvespace 2>/dev/null || true; "
                "pkill -f mpv 2>/dev/null || true; sleep 1"),
            shell=True, timeout=5)
        setup_controller.launch(["solvespace"])


class _ReviewedDesktopSetupOnly:
    """Evaluator-free projection for reviewed ordinary desktop task setups.

    The projection contains only public instruction metadata, hash-bound local
    setup assets, ordinary guest paths, and application launch actions.  It is
    deliberately not a ``BaseTask`` and has no ``evaluate`` method.
    """

    __slots__ = (
        "id", "instruction", "proxy", "config", "user_simulator",
        "evaluator", "disable_vnc", "disable_recording", "asset_bindings",
        "input_paths", "output_paths", "setup_kind", "state_asset",
        "chrome_spki_allowlist", "setup_text", "timed_updates",
    )

    def __init__(
            self, *, task_id: str, instruction: str, proxy: bool,
            disable_vnc: bool, disable_recording: bool,
            asset_bindings: tuple[tuple[str, str], ...],
            target_inputs: tuple[str, ...],
            candidate_outputs: tuple[str, ...], setup_kind: str,
            state_asset: str = "", setup_text: str = "",
            timed_updates: tuple[tuple[int, str], ...] = ()) -> None:
        self.id = task_id.removeprefix("task_")
        self.instruction = instruction
        self.proxy = proxy
        self.config: list[dict[str, Any]] = []
        self.user_simulator = None
        self.evaluator = None
        self.disable_vnc = disable_vnc
        self.disable_recording = disable_recording
        self.asset_bindings = asset_bindings
        self.input_paths = target_inputs
        self.output_paths = candidate_outputs
        self.setup_kind = setup_kind
        self.state_asset = state_asset
        self.chrome_spki_allowlist = ""
        self.setup_text = setup_text
        self.timed_updates = timed_updates

    @property
    def setup_projection(self) -> str:
        return f"{DESKTOP_SETUP_PROJECTION_VERSION}-{self.id}"

    @property
    def target_inputs(self) -> tuple[str, ...]:
        return self.input_paths

    @property
    def candidate_outputs(self) -> tuple[str, ...]:
        return self.output_paths

    @property
    def setup_manifest(self) -> dict[str, Any]:
        bound = {
            guest_path: _sha256_regular_local_file(Path(host_path))
            for host_path, guest_path in self.asset_bindings
        }
        if self.state_asset:
            bound["host-state.json"] = _sha256_regular_local_file(
                Path(self.state_asset))
        manifest = {
            "schema_version": 1,
            "projection": self.setup_projection,
            "task_id": f"task_{self.id}",
            "local_asset_sha256": bound,
            "target_inputs": list(self.target_inputs),
            "candidate_outputs": list(self.candidate_outputs),
            "setup_kind": self.setup_kind,
            "proxy": self.proxy,
            "disable_vnc": self.disable_vnc,
            "disable_recording": self.disable_recording,
        }
        if self.setup_text:
            manifest["setup_text_sha256"] = hashlib.sha256(
                self.setup_text.encode("utf-8")).hexdigest()
        if self.timed_updates:
            manifest["timed_updates"] = [
                {
                    "delay_secs": delay,
                    "content_sha256": hashlib.sha256(
                        content.encode("utf-8")).hexdigest(),
                }
                for delay, content in self.timed_updates
            ]
        return manifest

    def setup(self, setup_controller, use_proxy: bool = False) -> None:
        """Replay only the reviewed, ordinary solver-visible setup actions."""

        del use_proxy
        parents = sorted({str(Path(guest).parent)
                          for _host, guest in self.asset_bindings})
        if parents:
            setup_controller.execute(["mkdir", "-p", *parents])
        setup_controller.download([
            {"url": host, "path": guest}
            for host, guest in self.asset_bindings
        ])
        if self.setup_kind == "streamview-shotcut":
            from desktop_env.controllers.website import \
                prepare_stateful_website_urls

            setup_controller.launch(
                ["google-chrome", "--remote-debugging-port=1337"])
            setup_controller.launch([
                "socat", "tcp-listen:9222,fork", "tcp:localhost:1337"])
            urls = prepare_stateful_website_urls(
                app="studio.streamview", state=self.state_asset,
                cache_dir=setup_controller.cache_dir)
            setup_controller._chrome_open_tabs_setup(urls)
        elif self.setup_kind == "shotcut-montage":
            setup_controller.launch(["shotcut"])
            setup_controller._sleep_setup(3)
        elif self.setup_kind == "shotcut":
            setup_controller.launch(["shotcut"])
        elif self.setup_kind == "wps-presentation":
            setup_controller.launch(["wps", self.target_inputs[0]])
        elif self.setup_kind == "wps-spreadsheet-pdf":
            setup_controller.launch(["wps", self.target_inputs[0]])
            setup_controller._open_setup(path=self.target_inputs[1])
        elif self.setup_kind == "files-only":
            # Task084's normal public setup prepares REAPER's ordinary Effects
            # directory but deliberately does not launch an application.
            setup_controller.execute([
                "mkdir", "-p", "/home/user/.config/REAPER/Effects"])
        elif self.setup_kind == "browser-presentation-dynamic":
            desktop = "/home/user/Desktop"
            archive = f"{desktop}/broken_presentation.zip"
            app_dir = f"{desktop}/broken_presentation"
            requirements = f"{desktop}/presentation_requirements.txt"
            initial_b64 = base64.b64encode(
                self.setup_text.encode("utf-8")).decode("ascii")
            unpack_script = (
                "import base64,pathlib,shutil,sys;"
                "archive=pathlib.Path(sys.argv[1]);"
                "app=pathlib.Path(sys.argv[2]);"
                "requirements=pathlib.Path(sys.argv[3]);"
                "shutil.rmtree(app,ignore_errors=True);"
                "shutil.unpack_archive(str(archive),str(archive.parent));"
                "requirements.write_bytes(base64.b64decode(sys.argv[4]));"
                "archive.unlink(missing_ok=True);"
                "shutil.rmtree(archive.parent/'__MACOSX',ignore_errors=True)"
            )
            setup_controller.execute([
                "python3", "-c", unpack_script,
                archive, app_dir, requirements, initial_b64])
            setup_controller.launch(["xdg-open", app_dir])
            setup_controller.execute([
                "bash", "-lc",
                "python3 -m pip install -q websocket-client "
                ">/tmp/task089_pip.log 2>&1 || true",
            ])
            setup_controller.launch([
                "google-chrome", "--remote-debugging-port=1337",
                "--remote-allow-origins=*",
            ])
            setup_controller.launch([
                "socat", "tcp-listen:9222,fork", "tcp:localhost:1337"])
            server_script = (
                "import pathlib,subprocess,sys;"
                "log=open('/tmp/ref_server.log','ab',buffering=0);"
                "subprocess.Popen([sys.executable,'-m','http.server','8010'],"
                "cwd=sys.argv[1],stdin=subprocess.DEVNULL,stdout=log,"
                "stderr=subprocess.STDOUT,start_new_session=True)"
            )
            setup_controller.execute([
                "python3", "-c", server_script,
                app_dir + "/references",
            ])
            setup_controller._chrome_open_tabs_setup([
                "http://localhost:8010/reference_slide1.png",
                "http://localhost:8010/reference_slide2.png",
            ])
            for number, (delay, content) in enumerate(
                    self.timed_updates, start=1):
                encoded = base64.b64encode(
                    content.encode("utf-8")).decode("ascii")
                update_script = (
                    "import base64,pathlib,sys,time;"
                    "time.sleep(int(sys.argv[1]));"
                    "pathlib.Path(sys.argv[2]).write_bytes("
                    "base64.b64decode(sys.argv[3]))"
                )
                command = " ".join((
                    "nohup", "python3", "-c", shlex.quote(update_script),
                    shlex.quote(str(delay)), shlex.quote(requirements),
                    shlex.quote(encoded),
                    f">/tmp/task089_note{number}.log", "2>&1",
                    "</dev/null", "&",
                ))
                setup_controller.execute(["bash", "-lc", command])
        else:  # pragma: no cover - construction is closed over reviewed specs
            raise RuntimeError(f"unsupported detached setup kind: {self.setup_kind}")


def _install_paths() -> None:
    import dotenv

    # Task modules resolve asset(...) at import time. Load the repository's
    # configured local mirror before the detached Task056 class is inspected.
    dotenv.load_dotenv(OSWORLD / ".env", override=False)
    for path in (str(OSWORLD), str(FORGE)):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(OSWORLD)


def _parser() -> argparse.ArgumentParser:
    from explore.targeting import TASK_VISIBLE_TARGET_ACK

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True,
                        help="safe lineage basename under results/explore")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--scratch-lineage", action="store_true")
    action.add_argument("--fence-probe", action="store_true")
    action.add_argument("--evolve", action="store_true")
    parser.add_argument("--target-task-file", "--curriculum-target-task-file",
                        dest="target_task_file", required=True,
                        help="immutable target query shown first to bootstrap Actor")
    parser.add_argument("--target-task-id", default="task_056",
                        choices=tuple(EXECUTE_ACKS),
                        help="immutable normal task-visible target setup")
    parser.add_argument(
        "--bootstrap-seed-root", default="",
        help="existing safe lineage basename whose completed failed Q0 is "
             "imported before Curriculum starts")
    parser.add_argument(
        "--continual-seed-root", default="",
        help="closed target-converged lineage whose exact frozen durable "
             "memory is imported, with provenance, before the new target Q0")
    parser.add_argument("--task-visible-ack", required=True,
                        help=f"exact task-visible attestation: "
                             f"{TASK_VISIBLE_TARGET_ACK}")
    # Retained only so old command scanners and archived invocations remain
    # intelligible. It is not accepted as V12 authorization.
    parser.add_argument("--target-aware-ack", default="",
                        help=argparse.SUPPRESS)
    parser.add_argument("--execute", default="",
                        help="paid-Agent launch acknowledgement bound to the "
                             "selected target task")
    return parser


def _load_continual_seed(source_root: Path) \
        -> tuple[dict[str, bytes], dict[str, Any]]:
    """Load one closed pre-evaluation memory snapshot without score surfaces.

    The closure is researcher-side provenance. Only the small returned record
    and the exact durable-memory bytes cross into the descendant lineage; eval
    configs, grader reports, and downstream scores do not.
    """

    from explore.e15_state import (
        E15StateError, _safe_file_bytes, file_sha256, read_event_ledger,
        tree_hashes, tree_sha256, verify_target_pass_archives,
    )

    closure_path = source_root / "LINEAGE_CLOSED_BEFORE_OFFICIAL_EVAL.json"
    closure_raw = _safe_file_bytes(closure_path)
    try:
        closure = json.loads(closure_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E15StateError("continual source closure is not valid JSON") from exc
    if (not isinstance(closure, dict)
            or closure.get("schema_version") != 1
            or closure.get("closed_before_official_evaluation") is not True
            or closure.get("lineage") != str(source_root)
            or closure.get("task") != closure.get("target", {}).get("task_id")):
        raise E15StateError("continual source closure is malformed")

    try:
        source_real = source_root.resolve(strict=True)
        snapshot = Path(closure["memory_snapshot"])
        snapshot_real = snapshot.resolve(strict=True)
        manifest_path = Path(closure["memory_manifest"])
        manifest_real = manifest_path.resolve(strict=True)
    except (KeyError, OSError, RuntimeError) as exc:
        raise E15StateError("continual source paths are unavailable") from exc
    if (snapshot_real.parent != source_real
            or manifest_real.parent != source_real
            or manifest_real.name != snapshot_real.name + ".MANIFEST.sha256"):
        raise E15StateError("continual snapshot escaped its closed lineage")

    memory_hashes = tree_hashes(snapshot_real)
    memory_sha = tree_sha256(memory_hashes)
    if (not memory_hashes
            or closure.get("memory_snapshot_sha256") != memory_hashes
            or closure.get("source_guard", {}).get("memory") != memory_hashes
            or closure.get("source_episode", {}).get(
                "source_memory_tree_sha256") != memory_sha
            or closure.get("source_episode", {}).get(
                "frozen_memory_tree_sha256") != memory_sha):
        raise E15StateError("continual snapshot does not match its closure")
    if (file_sha256(manifest_real)
            != closure.get("memory_manifest_sha256")):
        raise E15StateError("continual memory manifest hash drifted")
    expected_manifest = "".join(
        f"{digest}  {name}\n" for name, digest in memory_hashes.items()
    ).encode("utf-8")
    if _safe_file_bytes(manifest_real) != expected_manifest:
        raise E15StateError("continual memory manifest content is noncanonical")

    state_files = closure.get("source_guard", {}).get("state_files")
    required_state = {
        "E15_EVENTS.jsonl", "E15_LINEAGE_READY.json",
        "E15_RUN_MANIFEST.json", "state.json",
    }
    if not isinstance(state_files, dict) or set(state_files) != required_state:
        raise E15StateError("continual closure lacks its source state guard")
    for name, digest in state_files.items():
        if file_sha256(source_real / name) != digest:
            raise E15StateError("continual source state drifted after closure")

    try:
        ready = json.loads(_safe_file_bytes(
            source_real / "E15_LINEAGE_READY.json"))
        state = json.loads(_safe_file_bytes(source_real / "state.json"))
        metadata = json.loads(_safe_file_bytes(
            source_real / "_target_input" / "metadata.json"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E15StateError("continual source state is not valid JSON") from exc
    instruction_sha = hashlib.sha256(_safe_file_bytes(
        source_real / "_target_input" / "instruction.txt")).hexdigest()
    target = closure["target"]
    source_episode = closure["source_episode"]
    if (ready.get("official_evaluator_ran") is not False
            or ready.get("status") != "ready_for_official_evaluation"
            or ready.get("memory", {}).get("files") != memory_hashes
            or ready.get("memory", {}).get("tree_sha256") != memory_sha
            or ready.get("event_ledger", {}).get("sha256")
            != state_files["E15_EVENTS.jsonl"]
            or ready.get("run_manifest", {}).get("sha256")
            != state_files["E15_RUN_MANIFEST.json"]
            or state.get("status") != "target_converged"
            or state.get("memory_tree_sha256") != memory_sha
            or metadata.get("task_id") != target.get("task_id")
            or metadata.get("instruction_sha256") != instruction_sha
            or target.get("instruction_sha256") != instruction_sha
            or source_episode.get("official_evaluator_ran") is not False):
        raise E15StateError("continual source is not a closed target-ready lineage")

    events = read_event_ledger(
        source_real / "E15_EVENTS.jsonl", require_target_gated=True)
    if not events or events[-1]["event_type"] != "CONVERGED":
        raise E15StateError("continual source lacks terminal convergence")
    converged = events[-1]
    pass_sha = converged["payload"].get("target_pass_event_sha256")
    passes = [row for row in events
              if row["event_type"] == "TARGET_TEST_PASSED"
              and row["status"] == "completed"
              and row["event_sha256"] == pass_sha]
    if (len(passes) != 1
            or converged["payload"].get(
                "frozen_memory_tree_sha256") != memory_sha
            or source_episode.get("target_pass_event_sha256") != pass_sha
            or source_episode.get("convergence_event_sha256")
            != converged["event_sha256"]):
        raise E15StateError("continual source convergence linkage is invalid")
    verify_target_pass_archives(source_real, passes[0])

    memory = {
        name: _safe_file_bytes(snapshot_real / name)
        for name in memory_hashes
    }
    record = {
        "schema_version": 1,
        "source_lineage": source_real.name,
        "source_task_id": target["task_id"],
        "source_target_instruction_sha256": instruction_sha,
        "source_convergence_event_sha256": converged["event_sha256"],
        "source_target_pass_event_sha256": pass_sha,
        "source_closure_sha256": hashlib.sha256(closure_raw).hexdigest(),
        "source_memory_snapshot": snapshot_real.name,
        "source_memory_manifest_sha256": closure["memory_manifest_sha256"],
        "memory_tree_sha256": memory_sha,
    }
    return memory, record


def _load_configs():
    from config.settings import load as load_config
    from core.actor import build_system

    configs = {role: load_config(str(path))
               for role, path in CONFIG_PATHS.items()}
    actor = configs["actor"]
    memory = configs["memory"]
    if vars(actor) != vars(memory) or build_system(actor) != build_system(memory):
        raise RuntimeError(
            "E15 Actor and memory-distillation effective systems must match")
    expected_models = {
        "actor": "z-ai/glm-5.2",
        "memory": "z-ai/glm-5.2",
        "curriculum": "moonshotai/kimi-k3",
        "verifier": "moonshotai/kimi-k3",
    }
    for role, cfg in configs.items():
        if cfg.model != expected_models[role]:
            raise RuntimeError(f"unexpected E15 {role} model: {cfg.model}")
        if (cfg.look_ensemble != 1
                or bool(getattr(cfg, "vision_model_2", ""))):
            raise RuntimeError(
                f"E15 {role} visual configuration is not single-eye")
        if (not cfg.agent_decided_stop or not cfg.practice_mode
                or cfg.independent_verify or cfg.history_keep_pairs != 0
                or getattr(cfg, "practice_stall_iters", 0) != 0
                or cfg.max_resumes != 0):
            raise RuntimeError(f"E15 {role} lifecycle config is not agent-owned")
    return configs


def _load_setup_only_target(task_id: str, expected_instruction: str):
    """Validate and load a reviewed evaluator-free setup projection."""

    if str(OSWORLD) not in sys.path:
        sys.path.insert(0, str(OSWORLD))
    from osworld_task_surface import (
        load_public_task_surface,
        read_public_env_value,
    )

    if task_id not in DETACHED_SETUP_TASKS:
        raise RuntimeError(f"the V12 detached setup does not support {task_id}")
    os.environ.setdefault(
        "OSWORLD_FILE_BASE_URL",
        read_public_env_value(OSWORLD, "OSWORLD_FILE_BASE_URL"),
    )
    surface = load_public_task_surface(OSWORLD, task_id)
    instruction = surface.instruction
    if instruction.encode("utf-8") != expected_instruction.encode("utf-8"):
        raise RuntimeError(
            "registered target bytes differ from the normal task instruction")
    class_path = surface.class_path
    if not class_path.is_file():
        raise RuntimeError("task-visible target class is unavailable")
    if not surface.has_setup:
        raise RuntimeError("task-visible target has no setup operation")
    common = {
        "instruction": instruction,
        "proxy": surface.proxy,
        "disable_vnc": surface.disable_vnc,
        "disable_recording": surface.disable_recording,
    }
    if task_id == "task_003":
        projection = _Task003SetupOnly(
            **common,
            asset_paths=(
                _local_task_asset(
                    "task_003/city.zip", task_label="Task003"),
                _local_task_asset(
                    "task_003/filter.zip", task_label="Task003"),
                _local_task_asset(
                    "task_003/weather_hongkong.pptx",
                    task_label="Task003"),
            ),
        )
    elif task_id == "task_019":
        desktop = "/home/user/Desktop"
        projection = _ReviewedDesktopSetupOnly(
            task_id=task_id, **common,
            asset_bindings=tuple(
                (_local_task_asset(f"task_019/{name}", task_label="Task019"),
                 f"{desktop}/{name}")
                for name in (
                    "Chiikawa_episode1.mp4", "Chiikawa_episode2.mp4",
                    "Chiikawa_episode3.mp4", "caption_requirement.pdf")),
            target_inputs=tuple(
                f"{desktop}/{name}" for name in (
                    "Chiikawa_episode1.mp4", "Chiikawa_episode2.mp4",
                    "Chiikawa_episode3.mp4", "caption_requirement.pdf")),
            candidate_outputs=tuple(
                f"{desktop}/{name}" for name in (
                    "Chiikawa_episode1_watermarked.mp4",
                    "Chiikawa_episode2_watermarked.mp4",
                    "Chiikawa_episode3_watermarked.mp4", "Chiikawa.mp4",
                    "cream_soup_w_scripts_EN.mp4",
                    "cream_soup_w_scripts_EN.srt")),
            setup_kind="streamview-shotcut",
            state_asset=_local_task_asset(
                "task_019/state.json", task_label="Task019"),
        )
    elif task_id == "task_042":
        materials = "/home/user/Desktop/raw_materials"
        projection = _ReviewedDesktopSetupOnly(
            task_id=task_id, **common,
            asset_bindings=tuple(
                (_local_task_asset(f"task_042/{name}", task_label="Task042"),
                 f"{materials}/{name}")
                for name in ("A_roll.mp4", "BGM.mp3", "Logo.png")),
            target_inputs=tuple(
                f"{materials}/{name}"
                for name in ("A_roll.mp4", "BGM.mp3", "Logo.png")),
            candidate_outputs=(
                "/home/user/Desktop/HoK_Montage/",),
            setup_kind="shotcut-montage",
        )
    elif task_id == "task_044":
        desktop = "/home/user/Desktop"
        projection = _ReviewedDesktopSetupOnly(
            task_id=task_id, **common,
            asset_bindings=((_local_task_asset(
                "task_044/promo_video.mp4", task_label="Task044"),
                f"{desktop}/promo_video.mp4"),),
            target_inputs=(f"{desktop}/promo_video.mp4",),
            candidate_outputs=(
                f"{desktop}/promo_video_v1.mp4",
                f"{desktop}/promo_video.mlt"),
            setup_kind="shotcut",
        )
    elif task_id == "task_056":
        projection = _Task056SetupOnly(
            **common,
            asset_urls=(
                _local_task056_asset("task_056/clip_1.mp4"),
                _local_task056_asset("task_056/clip_2.mp4"),
                _local_task056_asset("task_056/clip_3.mp4"),
                _local_task056_asset("task_056/BGM.mp3"),
                _local_task056_asset("task_056/OSWorld.mp4"),
            ),
            streamview_state=_local_task056_asset(
                "task_056/state-streamview.json"),
            tutorial_asset=_local_task056_asset(
                "task_056/shotcut_cinematic_titles_tutorial.mp4"),
            tutorial_id=TASK056_TUTORIAL_ID,
        )
    elif task_id == "task_063":
        output = "/home/user/Desktop/SiriDemo.pptx"
        projection = _ReviewedDesktopSetupOnly(
            task_id=task_id, **common,
            asset_bindings=((_local_task_asset(
                "task_063/SiriDemo.pptx", task_label="Task063"), output),),
            target_inputs=(output,), candidate_outputs=(output,),
            setup_kind="wps-presentation",
        )
    elif task_id == "task_080":
        desktop = "/home/user/Desktop"
        workbook = f"{desktop}/FY26_GTM_Planning_Model_Broken.xlsx"
        memo = f"{desktop}/GTM_Operating_Model_Change_Memo.pdf"
        projection = _ReviewedDesktopSetupOnly(
            task_id=task_id, **common,
            asset_bindings=(
                (_local_task_asset(
                    "task_080/FY26_GTM_Planning_Model_Broken.xlsx",
                    task_label="Task080"), workbook),
                (_local_task_asset(
                    "task_080/GTM_Operating_Model_Change_Memo.pdf",
                    task_label="Task080"), memo),
            ),
            target_inputs=(workbook, memo), candidate_outputs=(workbook,),
            setup_kind="wps-spreadsheet-pdf",
        )
    elif task_id == "task_084":
        desktop = "/home/user/Desktop"
        names = (
            "dry_voice.wav", "music_bed.wav", "processing_spec.txt",
            "regions.txt", "telephone_autopan.jsfx",
        )
        projection = _ReviewedDesktopSetupOnly(
            task_id=task_id, **common,
            asset_bindings=tuple(
                (_local_task_asset(
                    f"task_084/{name}", task_label="Task084"),
                 f"{desktop}/{name}")
                for name in names),
            target_inputs=tuple(f"{desktop}/{name}" for name in names),
            candidate_outputs=(
                f"{desktop}/telephone_autopan.jsfx",
                f"{desktop}/processed_teaser.flac",
                f"{desktop}/processed_teaser.render_stats.html",
            ),
            setup_kind="files-only",
        )
    elif task_id == "task_089":
        desktop = "/home/user/Desktop"
        app_dir = f"{desktop}/broken_presentation"
        initial_requirements = (
            "Initial note:\n\n"
            "- fix the presentation to match the reference screenshots on "
            "the Desktop\n"
            "- keep the clean light presentation look unless I overwrite "
            "this file later\n"
            "- please re-check this same file before you finalize, because "
            "I may update it\n"
        )
        timed_updates = (
            (45,
             "Quick design experiment:\n\n"
             "For the rehearsal build, try a more image-heavy layout:\n"
             "- use a darker page background instead of the current very "
             "light one\n"
             "- make the slide title more compact rather than oversized\n"
             "- make the figure side more dominant than the text side\n"
             "- let figures fill their area rather than preserving empty "
             "margins\n"
             "- keep the sidebar pinned while testing\n"),
            (120,
             "Please ignore the earlier rehearsal experiment.\n\n"
             "For the final build:\n"
             "- keep the clean light presentation look\n"
             "- the title should remain prominent and presentation-like\n"
             "- the text column should be slightly wider than the figure "
             "column\n"
             "- preserve generous spacing between text and figure\n"
             "- figures should be fully contained, centered, and never "
             "cropped\n"
             "- the sidebar should participate in layout rather than being "
             "pinned\n"
             "- the main content should not be absolutely offset\n"),
            (180,
             "Presenter follow-up:\n\n"
             "This will be used live. Left/right arrow keys need to work "
             "reliably, the active sidebar\n"
             "state should stay synchronized with the displayed slide, and "
             "slide changes should still\n"
             "feel smooth rather than jumping abruptly.\n"),
        )
        projection = _ReviewedDesktopSetupOnly(
            task_id=task_id, **common,
            asset_bindings=((_local_task_asset(
                "task_089/broken_presentation.zip", task_label="Task089"),
                f"{desktop}/broken_presentation.zip"),),
            target_inputs=(
                f"{app_dir}/index.html",
                f"{app_dir}/styles.css",
                f"{app_dir}/slides.json",
                f"{app_dir}/app.js",
                f"{app_dir}/references/reference_slide1.png",
                f"{app_dir}/references/reference_slide2.png",
                f"{app_dir}/assets/fig1.png",
                f"{app_dir}/assets/fig2.png",
                f"{app_dir}/assets/fig3.png",
                f"{desktop}/presentation_requirements.txt",
            ),
            candidate_outputs=(f"{app_dir}/",),
            setup_kind="browser-presentation-dynamic",
            setup_text=initial_requirements,
            timed_updates=timed_updates,
        )
    elif task_id == "task_094":
        projection = _Task094SetupOnly(
            **common,
            reference_asset=_local_task_asset(
                "task_094/ref-video.mp4", task_label="Task094"),
        )
    else:
        raise RuntimeError(f"unsupported detached target setup: {task_id}")
    if (hasattr(projection, "evaluate")
            or hasattr(projection.setup.__self__, "evaluate")):
        raise RuntimeError("detached setup-only projection exposes evaluate")
    return projection, class_path.resolve()


def _scrub_evaluation_state(environment) -> None:
    """Remove stale host evaluation state after setup and null resets."""

    environment.task_config = None
    environment.instruction = None
    environment.config = []
    environment.user_simulator = None
    environment.evaluator = None
    environment.metric = None
    environment.metric_conj = "and"
    environment.result_getter = None
    environment.expected_getter = None
    environment.metric_options = {}


def _boot_vm():
    from desktop_env.desktop_env import DesktopEnv
    from env.vm import VM

    class NoEvaluationDesktopEnv(DesktopEnv):
        def evaluate(self):  # pragma: no cover - the invariant is never call
            raise RuntimeError(
                "official evaluation is disabled inside V12 evolution")

    environment = NoEvaluationDesktopEnv(
        provider_name="docker", action_space="pyautogui", os_type="Ubuntu",
        screen_size=(1920, 1080), headless=True, require_a11y_tree=False,
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
        "gateway_leaf_spki_sha256":
            installation.gateway_leaf_spki_sha256,
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


def _reset_target_vm(
        vm, target: str, setup_only_target, egress_seal) -> dict:
    """Create, seal, then configure a normal evaluator-free target VM."""

    environment = getattr(vm, "env", None)
    reset = getattr(environment, "reset", None)
    if not callable(reset):
        return {"ok": False, "error": "VM environment has no reset boundary"}
    try:
        from desktop_env import desktop_env as desktop_env_module

        setup_only_target.chrome_spki_allowlist = \
            egress_seal.chrome_spki_allowlist
        # Stage 1 always recreates the outer container. The egress boundary is
        # then installed outside the sudo-capable guest before task setup or an
        # Agent is allowed to run.
        environment.is_environment_used = True
        reset(task_config=None)
        prepare_unsealed = getattr(setup_only_target, "prepare_unsealed", None)
        if callable(prepare_unsealed):
            prepare_unsealed(environment.setup_controller)
        installation = egress_seal.install(vm, mode="target")
        probe = egress_seal.probe(vm, installation)

        # Stage 2 reuses that exact sealed container. Disable DesktopEnv's
        # internal retry loop: a setup failure must return to this two-stage
        # boundary instead of silently creating an unsealed replacement.
        prior_retries = desktop_env_module.MAX_RETRIES
        desktop_env_module.MAX_RETRIES = 1
        try:
            environment.is_environment_used = False
            reset(task_config=setup_only_target)
        finally:
            desktop_env_module.MAX_RETRIES = prior_retries
        if str(getattr(environment, "instruction", "")) != target:
            return {"ok": False,
                    "error": "task-visible reset instruction drifted"}

        # Re-attest after the complete tutorial/Chrome setup. The provider must
        # still own the same outer container and exact gateway identity/rules;
        # then a second live guest probe establishes the boundary that the next
        # Actor will actually inherit.
        post_installation = egress_seal.install(vm, mode="target")
        post_probe = egress_seal.probe(vm, post_installation)
        boundary_fields = (
            "mode", "policy_sha256", "rules_sha256", "outer_container_id",
            "gateway_ip", "gateway_ports", "gateway_ca_sha256",
            "gateway_leaf_spki_sha256", "pinned_upstream_ips",
            "ipv6_enforcement",
        )
        if any(getattr(post_installation, field) != getattr(
                installation, field) for field in boundary_fields):
            raise RuntimeError(
                "target setup changed the sealed outer-container boundary")
        installation, probe = post_installation, post_probe
        _scrub_evaluation_state(environment)
        if hasattr(vm, "_conda"):
            vm._conda = ""
        owned = " ".join((
            "/home/user/evolution_project", "/home/user/project_next.md",
            "/home/user/curriculum_notes.md", "/home/user/actor_handoff.md",
            "/home/user/verifier_report.md", "/home/user/learning_diagnosis.md",
            "/home/user/.e15_actor_execution",
            "/home/user/.e15_original_project",
            "/home/user/e15_target_candidate", "/home/user/.memory",
        ))
        out = vm.run_command(
            f"rm -rf {owned}; echo E15_TARGET_CLEAN_RC=$?", timeout=120) or ""
        if "E15_TARGET_CLEAN_RC=0" not in out:
            return {"ok": False,
                    "error": "could not clean V12-owned target state"}
        return {"ok": True, "egress": _egress_receipt(installation, probe)}
    except Exception as exc:  # noqa: BLE001 - converted to durable infra state
        try:
            _scrub_evaluation_state(environment)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"fresh target reset failed: {exc}"}


def _reset_null_vm_sealed(
        vm, target_direction: str, egress_seal,
        prepare_unsealed=None) -> dict:
    """Create a tool-complete null VM, then deny all guest public egress.

    ``prepare_unsealed`` is a trusted host-side application provisioner.  It
    runs after the ordinary null reset but before the egress seal, without any
    Agent context, target asset, evaluator, or grading surface.  This keeps a
    task-visible application available to Curriculum-authored practice while
    preserving the sealed learning boundary seen by every Agent.
    """

    from explore.e15_loop import _reset_null_vm

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
                raise RuntimeError(
                    "null VM lacks a setup controller for trusted tools")
            prepare_unsealed(setup_controller)
        installation = egress_seal.install(vm, mode="null")
        probe = egress_seal.probe(vm, installation)
    except Exception as exc:  # noqa: BLE001 - fail closed at reset boundary
        return {"ok": False,
                "error": f"null pre-seal boundary failed: {exc}"}
    return {"ok": True, "egress": _egress_receipt(installation, probe)}


def _fence_record(vm, targeting, *, task_class_path: Path,
                  input_manifest: str, setup_only_target,
                  egress_receipt: dict[str, Any]) -> dict:
    from tools.exam_fence import GUEST_PROBE_CMDS, judge_guest_probe
    from explore.e15_egress import egress_policy_manifest

    outputs = {name: vm.run_command(command, timeout=60) or ""
               for name, command in GUEST_PROBE_CMDS}
    failures = judge_guest_probe(outputs)
    setup_manifest_sha256 = hashlib.sha256(json.dumps(
        setup_only_target.setup_manifest, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": 3,
        "experiment": EXPERIMENT,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": outputs,
        "failures": failures,
        "targeting": targeting.metadata,
        "task_visible_runtime": {
            "schema_version": 2,
            "task_id": targeting.task_id,
            "setup_projection": setup_only_target.setup_projection,
            "task_class_sha256": hashlib.sha256(
                task_class_path.read_bytes()).hexdigest(),
            "setup_manifest_sha256": setup_manifest_sha256,
            "input_manifest_sha256": hashlib.sha256(
                input_manifest.encode("utf-8")).hexdigest(),
            "candidate_outputs": list(setup_only_target.candidate_outputs),
            "official_evaluator_available": False,
            "egress": {
                "policy": egress_policy_manifest(),
                "installation_and_probe": egress_receipt,
            },
        },
    }


def _is_sha256(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _validate_target_egress_record(value: Any) -> None:
    """Validate the fence-time exact-origin seal and live guest probe."""

    from explore.e15_egress import (
        egress_policy_manifest, egress_policy_sha256,
    )

    if (not isinstance(value, dict)
            or set(value) != {"policy", "installation_and_probe"}
            or value.get("policy") != egress_policy_manifest()):
        raise RuntimeError("V12 fence egress policy is malformed or drifted")
    receipt = value.get("installation_and_probe")
    receipt_keys = {
        "schema_version", "mode", "policy_sha256", "rules_sha256",
        "gateway_ca_sha256", "gateway_leaf_spki_sha256",
        "pinned_upstream_ips", "ipv6_enforcement", "probe",
    }
    if (not isinstance(receipt, dict) or set(receipt) != receipt_keys
            or receipt.get("schema_version") != 1
            or receipt.get("mode") != "target"
            or receipt.get("policy_sha256") != egress_policy_sha256()
            or not _is_sha256(receipt.get("rules_sha256"))
            or not _is_sha256(receipt.get("gateway_ca_sha256"))
            or not _is_sha256(receipt.get("gateway_leaf_spki_sha256"))
            or receipt.get("ipv6_enforcement") not in {
                "ip6tables-plus-ebtables",
                "ebtables-authoritative-no-outer-ipv6",
            }):
        raise RuntimeError("V12 fence lacks a valid target egress installation")
    upstreams = receipt.get("pinned_upstream_ips")
    if not isinstance(upstreams, list) or not upstreams:
        raise RuntimeError("V12 fence lacks pinned StreamView upstreams")
    try:
        parsed_upstreams = [ipaddress.ip_address(value) for value in upstreams]
    except ValueError as exc:
        raise RuntimeError("V12 fence upstream IP is malformed") from exc
    if any(address.version != 4 or not address.is_global
           for address in parsed_upstreams):
        raise RuntimeError("V12 fence upstream is not public IPv4")
    probe = receipt.get("probe")
    expected_probe = {
        "mode": "target",
        "exact_streamview_https": "PASS",
        "wrong_sni_cotenant": "BLOCKED",
        "direct_ip_https": "BLOCKED",
        "public_dns": "BLOCKED",
        "outer_gateway_service": "BLOCKED",
        "public_non_http_tcp": "BLOCKED",
        "public_ipv6": "BLOCKED",
    }
    if (not isinstance(probe, dict)
            or set(probe) != {*expected_probe, "transcript_sha256"}
            or any(probe.get(key) != expected
                   for key, expected in expected_probe.items())
            or not _is_sha256(probe.get("transcript_sha256"))):
        raise RuntimeError("V12 fence live egress probe did not pass")


def _validate_v12_fence_record(
        path: Path, targeting, *, task_class_path: Path,
        setup_only_target) -> str:
    """Rebind evolution to the exact task-visible setup probed at P0."""

    if path.is_symlink() or not path.is_file():
        raise RuntimeError("V12 requires a real completed fence probe record")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("V12 fence probe record is unreadable") from exc
    expected_record_keys = {
        "schema_version", "experiment", "created_at_utc", "results",
        "failures", "targeting", "task_visible_runtime",
    }
    if (not isinstance(record, dict)
            or set(record) != expected_record_keys
            or record.get("schema_version") != 3
            or record.get("experiment") != EXPERIMENT):
        raise RuntimeError("V12 fence probe record is malformed")
    try:
        time.strptime(str(record.get("created_at_utc")), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("V12 fence timestamp is malformed") from exc
    from tools.exam_fence import GUEST_PROBE_CMDS, judge_guest_probe
    results = record.get("results")
    expected_probe_names = {name for name, _command in GUEST_PROBE_CMDS}
    if (not isinstance(results, dict)
            or set(results) != expected_probe_names
            or any(not isinstance(value, str) for value in results.values())
            or judge_guest_probe(results)
            or record.get("failures") != []
            or record.get("targeting") != targeting.metadata):
        raise RuntimeError("V12 fence failed or targets different query bytes")
    runtime = record.get("task_visible_runtime")
    runtime_keys = {
        "schema_version", "task_id", "setup_projection",
        "task_class_sha256", "setup_manifest_sha256",
        "input_manifest_sha256", "candidate_outputs",
        "official_evaluator_available", "egress",
    }
    if (not isinstance(runtime, dict) or set(runtime) != runtime_keys
            or runtime.get("schema_version") != 2):
        raise RuntimeError("V12 fence lacks task-visible runtime provenance")
    _validate_target_egress_record(runtime.get("egress"))
    expected_class_sha256 = hashlib.sha256(
        task_class_path.read_bytes()).hexdigest()
    expected_setup_sha256 = hashlib.sha256(json.dumps(
        setup_only_target.setup_manifest, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    expected_outputs = list(setup_only_target.candidate_outputs)
    if (runtime.get("task_id") != targeting.task_id
            or runtime.get("setup_projection")
            != setup_only_target.setup_projection
            or runtime.get("task_class_sha256") != expected_class_sha256
            or runtime.get("setup_manifest_sha256")
            != expected_setup_sha256
            or runtime.get("candidate_outputs") != expected_outputs
            or runtime.get("official_evaluator_available") is not False
            or hasattr(setup_only_target, "evaluate")
            or hasattr(setup_only_target.setup.__self__, "evaluate")):
        raise RuntimeError("task-visible runtime drifted after the fence probe")
    input_sha256 = runtime.get("input_manifest_sha256")
    if not _is_sha256(input_sha256):
        raise RuntimeError("V12 fence input-manifest hash is malformed")
    return input_sha256


def _close(environment) -> None:
    close = getattr(environment, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - best-effort after durable state
            log.exception("environment close failed")


def _terminal_event_state(ledger, event_type: str) -> dict[str, bool]:
    """Preserve the interrupted phase instead of falsely closing it.

    Phase-open rows are emitted by ``e15_evolve`` before work begins.  A
    handled infrastructure return adds the recovery obligation; quarantine or
    Curriculum stall keeps the last phase flags exactly as recorded.
    """
    rows = ledger.read()
    if not rows:
        raise RuntimeError("E15 terminal event requires prior lifecycle state")
    state = dict(rows[-1]["state"])
    if event_type == "INFRASTRUCTURE_INCIDENT":
        state["recovery_open"] = True
    return state


def main(argv: list[str] | None = None) -> int:
    _install_paths()
    parser = _parser()
    args = parser.parse_args(argv)

    from explore.commit import validate_instruction_corpus
    from explore.e15_state import (
        DEFAULT_EMERGENCY_POLICY, EVENT_LEDGER_FILENAME, E15StateError,
        EventLedger, RUN_MANIFEST_FILENAME, create_run_manifest, tree_hashes,
        tree_sha256, write_json_exclusive, write_readiness_record,
    )
    from explore.initialization import (
        EMPTY_MEMORY_MODE, InitializationError, initialize_empty_memory,
        read_memory_initialization,
    )
    from explore.targeting import (
        TargetingError, register_lineage, resolve_lineage_root,
    )

    try:
        root = Path(resolve_lineage_root(RESULTS, args.root))
        root.mkdir(parents=True, exist_ok=True)
        bootstrap_seed_root = ""
        continual_seed_memory = None
        continual_seed_record = None
        if args.bootstrap_seed_root and args.continual_seed_root:
            parser.error(
                "--bootstrap-seed-root and --continual-seed-root are mutually "
                "exclusive")
        if args.bootstrap_seed_root:
            if not args.evolve:
                parser.error(
                    "--bootstrap-seed-root is only valid with --evolve")
            bootstrap_seed_root = resolve_lineage_root(
                RESULTS, args.bootstrap_seed_root)
            if Path(bootstrap_seed_root) == root:
                raise RuntimeError(
                    "bootstrap seed must be a different lineage")
        if args.continual_seed_root:
            if not args.evolve:
                parser.error(
                    "--continual-seed-root is only valid with --evolve")
            continual_seed_root = Path(resolve_lineage_root(
                RESULTS, args.continual_seed_root))
            if continual_seed_root == root:
                raise RuntimeError(
                    "continual seed must be a different lineage")
            continual_seed_memory, continual_seed_record = \
                _load_continual_seed(continual_seed_root)
        target_input = Path(args.target_task_file).expanduser()
        if not target_input.is_absolute():
            target_input = FORGE / target_input
        targeting = register_lineage(
            root,
            target_input_file=target_input,
            acknowledgement=args.task_visible_ack,
            task_visible_id=args.target_task_id,
        )
        if args.target_aware_ack:
            raise RuntimeError(
                "the instruction-only acknowledgement cannot authorize V12")
        if not targeting.task_visible or not targeting.instruction.strip():
            raise RuntimeError("V12 requires a task-visible target")
        setup_only_target, task_class_path = _load_setup_only_target(
            args.target_task_id, targeting.instruction)
        from explore.e15_v12_loop import (
            configure_target_surface, target_candidate_outputs,
        )
        configure_target_surface(args.target_task_id)
        from explore import e15_v12_loop as v12_loop
        if (tuple(setup_only_target.target_inputs)
                != tuple(v12_loop.TARGET_INPUTS)
                or tuple(setup_only_target.candidate_outputs)
                != target_candidate_outputs()):
            raise RuntimeError(
                "detached setup and V12 target boundary disagree")
        validate_instruction_corpus(str(CORPUS))

        if args.scratch_lineage:
            if args.execute:
                parser.error("--execute is only valid with --evolve")
            initialization = initialize_empty_memory(root)
            print(json.dumps({
                "status": "scratch_initialized",
                "root": str(root),
                "target_sha256": targeting.instruction_sha256,
                "memory_mode": initialization.mode,
            }, indent=2))
            return 0

        initialization = read_memory_initialization(root)
        if initialization is None or initialization.mode != EMPTY_MEMORY_MODE:
            raise RuntimeError("E15 lineage is not registered as an empty start")

        if args.fence_probe:
            if args.execute:
                parser.error("--execute is only valid with --evolve")
            path = root / "fence_probe.json"
            if path.exists() or path.is_symlink():
                raise RuntimeError("fence probe record already exists")
            from explore.e15_egress import V12EgressSeal

            environment, vm = _boot_vm()
            egress_seal = V12EgressSeal()
            try:
                reset_result = _reset_target_vm(
                    vm, targeting.instruction, setup_only_target,
                    egress_seal)
                if not reset_result.get("ok"):
                    raise RuntimeError(str(reset_result.get("error")))
                egress_receipt = reset_result.get("egress")
                if not isinstance(egress_receipt, dict):
                    raise RuntimeError("target reset omitted its egress receipt")
                from explore.e15_v12_loop import _target_input_fingerprint
                input_manifest = _target_input_fingerprint(vm)
                record = _fence_record(
                    vm, targeting, task_class_path=task_class_path,
                    input_manifest=input_manifest,
                    setup_only_target=setup_only_target,
                    egress_receipt=egress_receipt)
            finally:
                _close(environment)
                egress_seal.close()
            write_json_exclusive(path, record)
            print(json.dumps({
                "status": "fence_pass" if not record["failures"] else "fence_fail",
                "root": str(root),
                "failures": record["failures"],
            }, indent=2))
            return 0 if not record["failures"] else 1

        execute_ack = EXECUTE_ACKS[args.target_task_id]
        if args.execute != execute_ack:
            parser.error(f"--evolve requires --execute {execute_ack}")
        if (root / RUN_MANIFEST_FILENAME).exists():
            raise RuntimeError(
                "E15 manifest already exists; automatic context recovery is not "
                "yet authorized for this runner")

        expected_input_manifest_sha256 = _validate_v12_fence_record(
            root / "fence_probe.json", targeting,
            task_class_path=task_class_path,
            setup_only_target=setup_only_target)
        configs = _load_configs()
        from explore.e15_egress import V12EgressSeal

        environment, vm = _boot_vm()
        egress_seal = V12EgressSeal()
        ledger = EventLedger(root / EVENT_LEDGER_FILENAME)
        try:
            create_run_manifest(
                root,
                targeting=targeting,
                initialization=initialization,
                config_paths=CONFIG_PATHS,
                configs=configs,
                argv=list(sys.argv if argv is None else [sys.argv[0], *argv]),
                repo_root=FORGE,
                runtime_files=RUNTIME_FILES,
                fence_path=root / "fence_probe.json",
                corpus_path=CORPUS,
                emergency_policy=DEFAULT_EMERGENCY_POLICY,
            )
            ledger.append(
                "EVOLUTION_STARTED", status="in_progress",
                state={"project_open": False, "memory_phase_open": False,
                       "recovery_open": False},
                payload={
                    "experiment": EXPERIMENT,
                    "target_sha256": targeting.instruction_sha256,
                },
            )

            from explore.e15_loop import E15Hooks
            from explore.e15_v12_loop import e15_v12_evolve
            prepare_null_tools = getattr(
                setup_only_target, "prepare_unsealed", None)
            hooks = E15Hooks(
                reset_vm=lambda active_vm, direction:
                    _reset_null_vm_sealed(
                        active_vm, direction, egress_seal,
                        prepare_unsealed=prepare_null_tools))
            result = e15_v12_evolve(
                vm, str(root), targeting.instruction,
                configs["actor"], configs["verifier"],
                configs["curriculum"], configs["memory"],
                reset_target_vm=lambda active_vm, target: _reset_target_vm(
                    active_vm, target, setup_only_target, egress_seal),
                corpus_path=str(CORPUS),
                hooks=hooks,
                event_sink=ledger.append,
                expected_input_manifest_sha256=
                    expected_input_manifest_sha256,
                bootstrap_seed_root=bootstrap_seed_root,
                continual_seed_memory=continual_seed_memory,
                continual_seed_record=continual_seed_record,
            )

            if result.status == "target_converged":
                memory_hashes = tree_hashes(root / "memory")
                rows = ledger.read()
                target_passes = [
                    row for row in rows
                    if row["event_type"] == "TARGET_TEST_PASSED"
                    and row["status"] == "completed"]
                if not target_passes:
                    raise RuntimeError(
                        "target convergence lacks a completed target-test PASS")
                target_pass = target_passes[-1]
                memory_digest = tree_sha256(memory_hashes)
                if target_pass["payload"].get(
                        "frozen_memory_tree_sha256") != memory_digest:
                    raise RuntimeError(
                        "passing target test was not run with promoted memory")
                promotion = ledger.append(
                    "MEMORY_PROMOTED", status="completed",
                    state={"project_open": False, "memory_phase_open": False,
                           "recovery_open": False},
                    payload={
                        "memory_tree_sha256": memory_digest,
                        "projects": result.projects,
                        "target_pass_event_sha256":
                            target_pass["event_sha256"],
                    },
                )
                rationale_hash = hashlib.sha256(
                    result.terminal_text.encode("utf-8")).hexdigest()
                ledger.append(
                    "CONVERGED", status="completed",
                    state={"project_open": False, "memory_phase_open": False,
                           "recovery_open": False},
                    payload={
                        "memory_promoted": True,
                        "latest_memory_promotion_sha256":
                            promotion["event_sha256"],
                        "convergence_rationale_sha256": rationale_hash,
                        "projects": result.projects,
                        "target_pass_event_sha256":
                            target_pass["event_sha256"],
                        "frozen_memory_tree_sha256": memory_digest,
                    },
                )
                ready = write_readiness_record(
                    root, loaded_configs=configs)
                print(json.dumps({
                    "status": "target_converged",
                    "projects": result.projects,
                    "memory_tree_sha256": ready["memory"]["tree_sha256"],
                    "readiness": str(root / "E15_LINEAGE_READY.json"),
                }, indent=2))
                return 0

            event_type = {
                "stalled": "SEARCH_STALLED",
                "quarantined": "BOUNDARY_QUARANTINED",
                "infra": "INFRASTRUCTURE_INCIDENT",
            }.get(result.status, "INFRASTRUCTURE_INCIDENT")
            ledger.append(
                event_type,
                status="yielded" if result.status == "stalled" else "failed",
                state=_terminal_event_state(ledger, event_type),
                payload={
                    "projects": result.projects,
                    "reason_sha256": hashlib.sha256(
                        result.reason.encode("utf-8")).hexdigest(),
                },
            )
            print(json.dumps({
                "status": result.status,
                "projects": result.projects,
                "reason": result.reason,
            }, indent=2))
            return 2
        finally:
            _close(environment)
            egress_seal.close()
    except (E15StateError, InitializationError, TargetingError, RuntimeError,
            OSError) as exc:
        log.error("E15 launch refused: %s", exc)
        return 2


def raise_system_exit(code: int) -> None:
    raise SystemExit(code)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: raise_system_exit(143))
    logging.basicConfig(
        level=logging.INFO, format="[%(levelname)s %(name)s] %(message)s")
    raise SystemExit(main())
