"""Persist the verified image without changing other Compose environment settings."""

import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def write_image_tag(path, image):
    path = Path(path)
    if not re.fullmatch(r"qasedak-development:[0-9a-f]{40}", image):
        raise ValueError("Invalid deployment image tag")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Compose environment must be a regular file")
    previous = path.stat() if path.exists() else None
    lines = path.read_text().splitlines() if previous else []
    lines = [line for line in lines
             if line.partition("=")[0].strip().removeprefix("export ").strip() != "QASEDAK_IMAGE_TAG"]
    lines.append("QASEDAK_IMAGE_TAG=" + image)
    fd, temporary = tempfile.mkstemp(prefix=".compose-env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            os.fchmod(target.fileno(), stat.S_IMODE(previous.st_mode) if previous else 0o600)
            if previous and os.geteuid() == 0:
                os.fchown(target.fileno(), previous.st_uid, previous.st_gid)
            target.write("\n".join(lines) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    write_image_tag(sys.argv[1], sys.argv[2])
