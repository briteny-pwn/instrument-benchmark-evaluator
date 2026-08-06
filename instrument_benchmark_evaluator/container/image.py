from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ContainerContract
from .docker_client import DockerClient
from .dockerfile import validate_dockerfile
from .errors import ImagePolicyError


@dataclass(frozen=True)
class ImageEvidence:
    image_reference: str
    image_id: str
    platform: str
    user: str
    dockerfile_sha256: str
    base_images: tuple[str, ...]
    repo_digests: tuple[str, ...]


def resolve_image(
    contract: ContainerContract,
    client: DockerClient,
    *,
    instance_id: str,
) -> ImageEvidence:
    dockerfile = validate_dockerfile(contract.dockerfile, contract)
    inspected = client.image_inspect(contract.lock.image_reference)
    image_id = _string(inspected.get("Id"), "image digest")
    if image_id != contract.lock.image_digest:
        raise ImagePolicyError(
            "image digest does not match image lock: "
            f"expected {contract.lock.image_digest}, got {image_id}"
        )
    platform = (
        f"{_string(inspected.get('Os'), 'image OS')}/"
        f"{_string(inspected.get('Architecture'), 'image architecture')}"
    )
    if platform != contract.platform:
        raise ImagePolicyError(
            f"image platform must be {contract.platform}, got {platform}"
        )
    config = inspected.get("Config")
    if not isinstance(config, dict):
        raise ImagePolicyError("image Config is missing")
    user = _string(config.get("User"), "image user")
    if user != contract.user:
        raise ImagePolicyError(
            f"image user must be {contract.user}, got {user}"
        )
    labels = config.get("Labels")
    if labels is not None:
        if not isinstance(labels, dict):
            raise ImagePolicyError("image labels are invalid")
        if labels.get("iab.instance") != instance_id:
            raise ImagePolicyError("image instance label does not match instance")
        if (
            labels.get("iab.dockerfile-sha256")
            != dockerfile.dockerfile_sha256
        ):
            raise ImagePolicyError("image Dockerfile label does not match lock")
    repo_digests = inspected.get("RepoDigests") or []
    if not isinstance(repo_digests, list) or not all(
        isinstance(value, str) for value in repo_digests
    ):
        raise ImagePolicyError("image RepoDigests must be a list of strings")
    return ImageEvidence(
        image_reference=contract.lock.image_reference,
        image_id=image_id,
        platform=platform,
        user=user,
        dockerfile_sha256=dockerfile.dockerfile_sha256,
        base_images=dockerfile.base_images,
        repo_digests=tuple(repo_digests),
    )


def build_image(
    contract: ContainerContract,
    client: DockerClient,
    *,
    instance_id: str,
    temporary_root: Path | None = None,
) -> ImageEvidence:
    validate_dockerfile(contract.dockerfile, contract)
    if temporary_root is None:
        with tempfile.TemporaryDirectory(prefix="iab-image-") as directory:
            return _build_in_context(
                contract,
                client,
                instance_id,
                Path(directory),
            )
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="iab-image-", dir=temporary_root
    ) as directory:
        return _build_in_context(
            contract,
            client,
            instance_id,
            Path(directory),
        )


def _build_in_context(
    contract: ContainerContract,
    client: DockerClient,
    instance_id: str,
    context: Path,
) -> ImageEvidence:
    for relative in contract.context_files:
        source = contract.instance_root / relative
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    client.run(
        [
            "build",
            "--build-arg=SOURCE_DATE_EPOCH=0",
            "--network=none",
            f"--platform={contract.platform}",
            "--load",
            "--provenance=false",
            "--tag",
            contract.lock.image_reference,
            "--file",
            str(context / contract.dockerfile.relative_to(contract.instance_root)),
            str(context),
        ]
    )
    return resolve_image(contract, client, instance_id=instance_id)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImagePolicyError(f"{label} is missing")
    return value
