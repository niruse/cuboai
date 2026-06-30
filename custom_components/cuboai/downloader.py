import asyncio
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


async def _get_docker_token(session: aiohttp.ClientSession) -> str:
    """Get anonymous pull token for wyze-bridge from Docker Hub."""
    async with session.get(DOCKER_AUTH) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["token"]


async def _download_tutk_libs(arch: str, dest_dir: str):
    """Download TUTK libraries from wyze-bridge docker image."""
    docker_arch = "amd64" if arch == "x86_64" else "arm64"

    os.makedirs(dest_dir, exist_ok=True)

    # Target files
    targets = ["libIOTCAPIs_ALL.so"]

    # Check if we already have them
    have_all = True
    for target in targets:
        if not os.path.exists(os.path.join(dest_dir, target)):
            have_all = False
            break
    if have_all:
        _LOGGER.debug(f"TUTK libraries for {arch} already downloaded.")
        return

    _LOGGER.info(f"Downloading TUTK native libraries for {arch}...")

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
        found_count = 0

        for layer in layers:
            if found_count == len(targets):
                break

            layer_digest = layer["digest"]
            _LOGGER.debug(f"Downloading layer {layer_digest}...")

            async with session.get(
                f"{DOCKER_REGISTRY}/mrlt8/wyze-bridge/blobs/{layer_digest}", headers=headers
            ) as resp:
                resp.raise_for_status()
                tar_bytes = await resp.read()

                # Extract target files from tarball
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
                    for member in tar.getmembers():
                        basename = os.path.basename(member.name)
                        if basename in targets:
                            out_path = os.path.join(dest_dir, basename)
                            with open(out_path, "wb") as f:
                                f.write(tar.extractfile(member).read())
                            _LOGGER.debug(f"Extracted {basename}")
                            found_count += 1
                            if found_count == len(targets):
                                break

        if found_count < len(targets):
            raise RuntimeError(f"Could not find all TUTK libraries in docker image for {arch}")


async def _download_go2rtc(arch: str, dest_dir: str):
    """Download go2rtc binary from GitHub."""
    os.makedirs(dest_dir, exist_ok=True)
    binary_path = os.path.join(dest_dir, "go2rtc")

    if os.path.exists(binary_path):
        _LOGGER.debug("go2rtc binary already downloaded.")
        return

    go2rtc_arch = "amd64" if arch == "x86_64" else "arm64"
    url = f"https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_{go2rtc_arch}"

    _LOGGER.info(f"Downloading go2rtc for {go2rtc_arch}...")

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            content = await resp.read()

            with open(binary_path, "wb") as f:
                f.write(content)

            # Make executable
            st = os.stat(binary_path)
            os.chmod(binary_path, st.st_mode | stat.S_IEXEC)
            _LOGGER.debug("go2rtc downloaded and marked as executable.")


async def async_ensure_dependencies(hass) -> bool:
    """Ensure all proprietary Native libraries and go2rtc are downloaded."""
    # Determine arch
    machine = platform.machine().lower()
    if machine in ["x86_64", "amd64"]:
        arch = "x86_64"
    elif machine in ["aarch64", "arm64"]:
        arch = "aarch64"
    else:
        _LOGGER.error(f"Unsupported architecture for CuboAI TUTK local control: {machine}")
        return False

    base_dir = os.path.dirname(__file__)
    libs_dir = os.path.join(base_dir, "libs", arch)
    bin_dir = os.path.join(base_dir, "bin")

    try:
        await asyncio.gather(_download_tutk_libs(arch, libs_dir), _download_go2rtc(arch, bin_dir))
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to download CuboAI native dependencies: {e}")
        return False
