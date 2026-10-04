"""Independent immutable raw and research publication pointers for Japan."""

import json
import uuid
from pathlib import Path


def publication_path(root, *, raw=False):
    root = Path(root).resolve()
    pointer = root / ("raw-current.json" if raw else "current.json")
    if raw and not pointer.is_file():
        pointer = root / "current.json"  # Existing publications remain readable.
    if not pointer.is_file():
        return root
    metadata = json.loads(pointer.read_text(encoding="utf-8"))
    selected = (root / metadata["path"]).resolve()
    if (
        not selected.is_relative_to(root / "versions")
        or not (selected / "manifest.json").is_file()
        or selected.name != metadata["version"]
    ):
        raise ValueError("JP publication pointer is invalid/incomplete")
    return selected


def publish_pointer(root, version, *, raw=False):
    root = Path(root)
    pointer = root / (".pointer-" + uuid.uuid4().hex + ".json")
    pointer.write_text(
        json.dumps({"version": version, "path": "versions/" + version}),
        encoding="utf-8",
    )
    pointer.replace(root / ("raw-current.json" if raw else "current.json"))


def publication_status(root):
    raw = publication_path(root, raw=True)
    research = publication_path(root)
    manifest = json.loads((research / "manifest.json").read_text("utf-8"))
    parent = manifest.get("parent_version", research.name)
    return {
        "raw_version": raw.name,
        "research_version": research.name,
        "research_ready": bool(manifest.get("datasets", {}).get("l1_factors")),
        "research_update_pending": parent != raw.name,
    }


def open_raw_hub():
    from .quantjp_hub import QuantJPDataHub, _resolve_quantjp_data_dir

    return QuantJPDataHub(publication_path(_resolve_quantjp_data_dir(), raw=True))
