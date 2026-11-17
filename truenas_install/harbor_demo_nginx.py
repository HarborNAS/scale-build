#!/usr/bin/env python3
"""Retain the accepted Demo UI/edge while upgrading the native platform.

This is a versioned migration adapter, not the formal HarborGate integration.
The existing 4176 edge verifies each NAS user token and reconstructs identity.
External legacy Gate setup/control is disabled; its loopback IM runtime remains.
The formal middleware template and the retained r4 release are never modified.
"""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


MODE = "retained-demo-edge-v1-external-gate-disabled"
RELEASE = "photo-demo-20261117-r4"
RELEASE_ROOT = "/mnt/Test_Pool2/HarborDemo20261120/releases/" + RELEASE
HELPER_PATH = "usr/local/libexec/harbor-demo-nginx-compat.py"
SERVICE_PATH = "etc/systemd/system/harbor-photo-demo-nginx.service"
NGINX_DROPIN_PATH = "etc/systemd/system/nginx.service.d/40-harbor-demo-compat.conf"
SHAPES = (
    "= /api/harbor-beacon", "^~ /api/harbor-beacon/",
    "= /api/harbor-gate", "^~ /api/harbor-gate/",
)
IDENTITY_HEADERS = (
    "X-Harbor-Principal-Id", "X-Harbor-Principal-Source", "X-Harbor-Principal-Roles",
    "X-Harbor-Camera-Scope", "X-Harbor-Workspace-Id", "X-Harbor-User-Id", "X-Harbor-Edge-Assertion",
)
LOCATION = re.compile(r"(?m)^(?P<indent>[ \t]*)location[ \t]+(?P<shape>[^\n{]+?)\s*\{(?P<body>[^{}]*)\}")
PRIVATE_ENDPOINT = re.compile(r":(?:4174|4176|8787)(?=[/;\s\"'?#])")


def _directive(body, name):
    matches = list(re.finditer(r"(?m)^([ \t]*)" + re.escape(name) + r"[ \t]+([^;\n]+);[ \t]*$", body))
    if len(matches) != 1:
        raise ValueError("Expected exactly one " + name)
    return matches[0]


def _require(body, name, value):
    match = _directive(body, name)
    if match[2].strip() != value:
        raise ValueError("Unexpected " + name)
    return match


def _location(match):
    shape, body = match["shape"].strip(), match["body"]
    for name in IDENTITY_HEADERS:
        _require(body, "proxy_set_header " + name, '""')
    _require(body, "proxy_set_header X-HarborOS-Auth-Token", "$http_x_harboros_auth_token")
    _require(body, "limit_req", "zone=harbor_assistant burst=40 nodelay")
    _require(body, "limit_req_status", "429")
    auth = _directive(body, "proxy_set_header Authorization")
    if "harbor-beacon" in shape:
        target = _directive(body, "proxy_pass")
        original = "http://127.0.0.1:8787/api/beacon" + ("/" if shape.endswith("/") else "")
        if (target[2], auth[2]) not in ((original, '""'), ("http://127.0.0.1:4176", "$http_authorization")):
            raise ValueError("Unexpected Beacon authentication route")
        body = body[:target.start(2)] + "http://127.0.0.1:4176" + body[target.end(2):]
        auth = _directive(body, "proxy_set_header Authorization")
        body = body[:auth.start(2)] + "$http_authorization" + body[auth.end(2):]
        body = body.replace(
            "Gate validates this one-time NAS token; user-supplied trusted identities are stripped.",
            "Retained Demo edge validates the NAS Authorization token; caller identities stay stripped.",
        )
    else:
        if auth[2] != '""':
            raise ValueError("Unexpected legacy Gate authorization")
        if re.search(r"(?m)^\s*proxy_pass\s", body):
            target = _require(body, "proxy_pass", "http://127.0.0.1:8787")
            if re.search(r"(?m)^\s*return\s", body):
                raise ValueError("Ambiguous legacy Gate route")
            body = body[:target.start()] + target[1] + "return 404;" + body[target.end():]
        else:
            _require(body, "return", "404")
        body = body.replace(
            "Gate validates this one-time NAS token; user-supplied trusted identities are stripped.",
            "Legacy Demo Gate has no NAS authentication: external setup/control stays disabled.",
        )
    return match[0][:match.start("body") - match.start()] + body + "}"


def render_nginx(text):
    """Adapt only the reviewed native locations; reject other private API routes."""
    matches = [
        m for m in LOCATION.finditer(text)
        if "/api/harbor-beacon" in m["shape"] or "/api/harbor-gate" in m["shape"]
    ]
    declarations = re.findall(r"(?m)^\s*location[^\n{]*/api/harbor-(?:beacon|gate)[^\n{]*\{", text)
    shapes = [m["shape"].strip() for m in matches]
    if not shapes or len(declarations) != len(matches) or shapes != list(SHAPES) * (len(shapes) // 4):
        raise ValueError("Expected native Beacon/Gate location pairs in every server")
    outside = text
    for match in reversed(matches):
        outside = outside[:match.start()] + outside[match.end():]
    # Also reject named upstreams and variables pointing at these private ports.
    if PRIVATE_ENDPOINT.search(outside):
        raise ValueError("Unreviewed public proxy to a private Harbor service")
    for match in reversed(matches):
        text = text[:match.start()] + _location(match) + text[match.end():]
    text = text.replace(
        "# Compatibility URL now goes through the same verified Gate API as the\n"
        "        # explicit /api/harbor-gate/api/beacon path. Beacon stays private on loopback.",
        "# Retained Demo compatibility: NAS-authenticated edge only; external legacy Gate disabled.\n"
        "        # Formal HarborGate integration remains the middleware template default.",
    )
    roots = re.compile(
        r"(?m)^([ \t]*(?:root|alias)[ \t]+)(?:/usr/share/truenas/webui|"
        + re.escape(RELEASE_ROOT + "/ui") + r")(?=[/;])"
    )
    text, count = roots.subn(lambda m: m[1] + RELEASE_ROOT + "/ui", text)
    if not count:
        raise ValueError("Expected the native or retained Demo WebUI root")
    return text


def migration_files(source_service_text):
    """Return the explicit new-BE writes; the source BE/r4 stays immutable."""
    original = (
        "ExecStart=/usr/bin/python3 " + RELEASE_ROOT + "/deploy_photo_demo.py reconcile-nginx --release " + RELEASE
    )
    starts = re.findall(r"(?m)^ExecStart=.*$", source_service_text)
    if starts != [original]:
        raise ValueError("Unrecognized retained Demo nginx service")
    replacement = "ExecStart=/usr/bin/python3 /" + HELPER_PATH + " --release " + RELEASE
    service = "# Harbor compatibility mode: " + MODE + "\n" + source_service_text.replace(original, replacement)
    command = "/usr/bin/python3 /" + HELPER_PATH + " --release " + RELEASE + " --render-only"
    # Preserve the reviewed native nginx commands. A failed helper prevents the
    # subsequent start/reload command, including middleware-triggered reloads.
    dropin = f"""# Harbor compatibility mode: {MODE}
[Service]
ExecStartPre=
ExecStartPre={command}
ExecStartPre=/usr/sbin/nginx -t -q -g 'daemon on; master_process on;'
ExecReload=
ExecReload={command}
ExecReload=/usr/sbin/nginx -g 'daemon on; master_process on;' -s reload
"""
    return {
        HELPER_PATH: Path(__file__).read_bytes(), SERVICE_PATH: service.encode("utf-8"),
        NGINX_DROPIN_PATH: dropin.encode("utf-8"),
    }


@contextmanager
def _configuration_lock(config):
    # The installer and runtime are Linux. Keep this import local so renderer
    # and unit-generation tests can also run on a non-Linux development host.
    import fcntl

    path = config.with_name(".harbor-demo-nginx-compat.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("nginx compatibility lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _render_configuration(config, run):
    if config.is_symlink() or not stat.S_ISREG(config.stat().st_mode):
        raise ValueError("nginx configuration must be a regular file")
    original = config.read_text()
    candidate = render_nginx(original)
    if candidate == original:
        return False
    info = config.stat()
    fd, temporary = tempfile.mkstemp(prefix="nginx.harbor-demo-", suffix=".conf", dir=config.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(candidate)
        temporary.chmod(stat.S_IMODE(info.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, info.st_uid, info.st_gid)
        run(["nginx", "-t", "-c", str(temporary)], check=True)
        if config.is_symlink() or config.read_text() != original:
            raise ValueError("nginx configuration changed during reconciliation")
        temporary.replace(config)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def reconcile_nginx(config=Path("/etc/nginx/nginx.conf"), run=subprocess.run, *, render_only=False):
    with _configuration_lock(config):
        changed = _render_configuration(config, run)
    # Release before systemctl: ExecReload invokes this helper again under the
    # same lock. Native start/reload hooks only render and never call systemctl.
    if changed and not render_only:
        run(["systemctl", "reload", "nginx.service"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, choices=[RELEASE])
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    if not Path(RELEASE_ROOT + "/ui/index.html").is_file():
        raise SystemExit("Retained Demo UI is unavailable")
    reconcile_nginx(render_only=args.render_only)
