"""F4.2 (FR-30): which release this server IS.

Every product image records its own identity at build time
(`deploy/compose/gentlefleet.Dockerfile`, F1.4): the release version, the
product commit, the RMF pin's sha256 and the upstream patches, one per line
in /opt/gentlefleet/VERSION, with GF_VERSION / GF_GIT_SHA also in the
environment. The same values are the images' OCI labels, so what this
module serves can be checked against what `docker inspect` says.

Read once per request (the file is four lines); never cached, so a server
that is somehow running from a different image than it thinks says so.
Outside an image — a developer's checkout, the test suite — there is no
file and no environment: the identity is "dev", and `source` says why.
"""

import os
from pathlib import Path
from typing import Dict

VERSION_FILE = Path("/opt/gentlefleet/VERSION")
UNKNOWN = "unknown"


def build_identity(version_file: Path = VERSION_FILE) -> Dict[str, str]:
    """{version, git_sha, rmf_pin_sha256, patches, source}."""
    identity = {
        "version": "",
        "git_sha": "",
        "rmf_pin_sha256": "",
        "patches": "",
        "source": "",
    }
    try:
        lines = version_file.read_text().splitlines()
    except OSError:
        lines = []
    if lines:
        identity["source"] = str(version_file)
        identity["version"] = lines[0].strip()
        if len(lines) > 1:
            identity["git_sha"] = lines[1].strip()
        for line in lines[2:]:
            key, _, value = line.partition("=")
            if key.strip() in ("rmf_pin_sha256", "patches"):
                identity[key.strip()] = value.strip()
    if not identity["version"]:
        env_version = os.environ.get("GF_VERSION", "").strip()
        if env_version:
            identity["version"] = env_version
            identity["git_sha"] = os.environ.get("GF_GIT_SHA", "").strip()
            identity["source"] = "environment (GF_VERSION)"
    if not identity["version"]:
        identity["version"] = "dev"
        identity["source"] = (
            "no release record — not running from a release image "
            "(no /opt/gentlefleet/VERSION, no GF_VERSION)"
        )
    for key in ("git_sha", "rmf_pin_sha256", "patches"):
        identity[key] = identity[key] or UNKNOWN
    return identity
