import io
import logging
import os
import platform
import stat
import tarfile

import aiohttp

_LOGGER = logging.getLogger(__name__)

DOCKER_REGISTRY = "https://registry-1.docker.io/v2"
DOCKER_AUTH = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:mrlt8/wyze-bridge:pull"

GO2RTC_REPO = "AlexxIT/go2rtc"

# HA host architecture -> go2rtc release-asset architecture.
# go2rtc publishes builds for far more platforms than the native TUTK lib,
# and the pure-Python transport needs no native lib at all — so every arch
# here gets full local streaming.
_GO2RTC_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
    "arm": "arm",
    "i386": "386",
    "i686": "386",
}

# The optional native TUTK lib (extracted from the wyze-bridge Docker image)
# only exists for these two; everywhere else the pure backend is used.
_TUTK_DOCKER_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


async def _get_docker_token(session: aiohttp.ClientSession) -> str:
    """Get anonymous pull token for wyze-bridge from Docker Hub."""
    async with session.get(DOCKER_AUTH) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["token"]


async def _download_tutk_libs(hass, docker_arch: str, dest_dir: str):
    """Download TUTK libraries from wyze-bridge docker image."""
    await hass.async_add_executor_job(os.makedirs, dest_dir, 0o777, True)

    # Target files
    targets = ["libIOTCAPIs_ALL.so"]

    def _have_all():
        return all(os.path.exists(os.path.join(dest_dir, t)) for t in targets)

    if await hass.async_add_executor_job(_have_all):
        _LOGGER.debug("TUTK libraries already downloaded.")
        return

    _LOGGER.info(f"Downloading TUTK native libraries ({docker_arch})...")

    async with aiohttp.ClientSession() as session:
        token = await _get_docker_token(session)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Get manifest list
        headers["Accept"] = (
            "application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json"
        )

        async with session.get(f"{DOCKER_REGISTRY}/mrlt8/wyze-bridge/manifests/latest", headers=headers) as resp:
            resp.raise_for_status()
            manifest = await resp.json()

        # 2. Find correct architecture manifest digest
        digest = None
        if "manifests" in manifest:
            for m in manifest["manifests"]:
                if m.get("platform", {}).get("architecture") == docker_arch:
                    digest = m["digest"]
                    break

            if not digest:
                raise RuntimeError(f"Could not find Docker manifest for architecture {docker_arch}")

            headers["Accept"] = "application/vnd.docker.distribution.manifest.v2+json"
            async with session.get(f"{DOCKER_REGISTRY}/mrlt8/wyze-bridge/manifests/{digest}", headers=headers) as resp:
                resp.raise_for_status()
                manifest = await resp.json()

        # 3. We assume the libraries are in the last layer (app layer)
        # We will iterate backwards through the layers to find the files
        layers = reversed(manifest.get("layers", []))
        found = []

        for layer in layers:
            if len(found) == len(targets):
                break

            layer_digest = layer["digest"]
            _LOGGER.debug(f"Downloading layer {layer_digest}...")

            async with session.get(
                f"{DOCKER_REGISTRY}/mrlt8/wyze-bridge/blobs/{layer_digest}", headers=headers
            ) as resp:
                resp.raise_for_status()
                tar_bytes = await resp.read()

            def _extract(blob=tar_bytes):
                # gunzip + tar scan is CPU/disk heavy — run in the executor,
                # never on the event loop.
                extracted = []
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                    for member in tar.getmembers():
                        basename = os.path.basename(member.name)
                        if basename in targets and basename not in found:
                            out_path = os.path.join(dest_dir, basename)
                            tmp_path = out_path + ".tmp"
                            with open(tmp_path, "wb") as f:
                                f.write(tar.extractfile(member).read())
                            os.replace(tmp_path, out_path)
                            extracted.append(basename)
                            if len(found) + len(extracted) == len(targets):
                                break
                return extracted

            found.extend(await hass.async_add_executor_job(_extract))

        if len(found) < len(targets):
            raise RuntimeError(f"Could not find all TUTK libraries in docker image for {docker_arch}")


async def _download_go2rtc(hass, go2rtc_arch: str, dest_dir: str):
    """Download go2rtc binary from GitHub."""
    binary_path = os.path.join(dest_dir, "go2rtc")

    def _exists():
        os.makedirs(dest_dir, exist_ok=True)
        return os.path.exists(binary_path)

    if await hass.async_add_executor_job(_exists):
        _LOGGER.debug("go2rtc binary already downloaded.")
        return

    url = f"https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_{go2rtc_arch}"

    _LOGGER.info(f"Downloading go2rtc for {go2rtc_arch}...")

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            content = await resp.read()

    def _write():
        # Write to a temp file and atomically rename so an interrupted
        # download can never leave a truncated binary that the existence
        # check above would then treat as valid forever.
        tmp_path = binary_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(content)
        st = os.stat(tmp_path)
        os.chmod(tmp_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp_path, binary_path)

    await hass.async_add_executor_job(_write)
    _LOGGER.debug("go2rtc downloaded and marked as executable.")


async def async_ensure_dependencies(hass) -> bool:
    """Ensure the go2rtc binary (required for streaming) and the optional
    native TUTK library are downloaded.

    Returns True when go2rtc is available. The native TUTK lib is best-effort:
    the pure-Python transport covers every architecture without it.
    """
    machine = platform.machine().lower()
    go2rtc_arch = _GO2RTC_ARCH.get(machine)
    tutk_arch = _TUTK_DOCKER_ARCH.get(machine)

    if not go2rtc_arch:
        _LOGGER.warning(
            "No go2rtc build known for architecture %s — local streaming disabled "
            "(camera control still works via the pure-Python transport).",
            machine,
        )

    # Check for Alpine Linux and install gcompat so the (optional) native
    # glibc TUTK library can load
    if tutk_arch and os.path.exists("/sbin/apk"):

        def _install_gcompat():
            try:
                _LOGGER.debug("Alpine Linux detected. Ensuring gcompat is installed...")
                os.system("apk add --no-cache gcompat >/dev/null 2>&1")
                import ctypes

                # Pre-load it into global namespace so ctypes can find the symbols later
                try:
                    ctypes.CDLL("libgcompat.so.0", mode=ctypes.RTLD_GLOBAL)
                except Exception:
                    pass
            except Exception as e:
                _LOGGER.warning(f"Failed to install gcompat: {e}")

        await hass.async_add_executor_job(_install_gcompat)

    base_dir = os.path.dirname(__file__)
    libs_dir = os.path.join(base_dir, "libs", machine)
    bin_dir = os.path.join(base_dir, "bin")

    go2rtc_ok = not go2rtc_arch  # nothing to do counts as "not failed"
    if go2rtc_arch:
        try:
            await _download_go2rtc(hass, go2rtc_arch, bin_dir)
            go2rtc_ok = True
        except Exception as e:
            _LOGGER.error(f"Failed to download go2rtc: {e}")

    if tutk_arch:
        try:
            await _download_tutk_libs(hass, tutk_arch, libs_dir)
        except Exception as e:
            # Optional: the pure-Python backend needs no native library.
            _LOGGER.info(f"Native TUTK library unavailable ({e}); using pure-Python transport.")

    return go2rtc_ok
