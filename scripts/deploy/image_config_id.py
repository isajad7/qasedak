"""Read the portable image configuration ID from a docker-save archive."""

import hashlib
import json
from pathlib import PurePosixPath
import sys
import tarfile


def image_config_id(archive_path, image_tag):
    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        matches = [entry for entry in manifest if image_tag in (entry.get("RepoTags") or [])]
        if len(matches) != 1:
            raise ValueError("Expected exactly one exported image matching the release tag")
        path = PurePosixPath(matches[0]["Config"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Invalid image configuration path")
        member = archive.getmember(str(path))
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("Invalid image configuration blob")
        blob = archive.extractfile(member).read()
        config = json.loads(blob)
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("Exported image must target linux/amd64")
        return "sha256:" + hashlib.sha256(blob).hexdigest()


if __name__ == "__main__":
    print(image_config_id(sys.argv[1], sys.argv[2]))
