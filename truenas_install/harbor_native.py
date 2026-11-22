"""Opt-in new-BE conversion from the Demo units to the native Nexus packages."""

import json
import hashlib
import os
import re
import shlex
import shutil

from .harbor_state import (
    BE_MNT_PATHS, CONFIG_PATHS, NATIVE_MODE, NGINX_DROPIN, NGINX_HELPER,
    Entry, HarborMigrationError, _digest, _safe_path, inventory, validate_mounts,
)

BACKUP = "var/lib/harboros-upgrade/native-legacy-config"
GATE_ROOT = "var/lib/harboros-im-gate"
GATE_WRITABLE_PATHS = {
    "IM_AGENT_DATA_DIR": "/" + GATE_ROOT + "/sessions",
    "IM_AGENT_STATE_DIR": "/" + GATE_ROOT,
    "HARBORGATE_DEVICE_SESSION_STATE_DIR": "/" + GATE_ROOT + "/device-sessions",
    "WEIXIN_STATE_DIR": "/" + GATE_ROOT + "/weixin",
    "FEISHU_MAIL_TOKEN_STATE_PATH": "/" + GATE_ROOT + "/feishu-mail-token.json",
    "FEISHU_SETUP_PORTAL_PATH": "/" + GATE_ROOT + "/_setup_portal.json",
}
CORE = ("harboros-beacon.service", "harboros-im-gate.service")
RETIRED = ("harbor-gate-webui-edge.service", "harbor-photo-demo-nginx.service", "harbor-photo-demo-nginx.path")
ENV_FILES = tuple(path for path in CONFIG_PATHS if path.startswith("etc/default/"))
AUTH_PREFIXES = (
    "HARBOR_GATE_TO_BEACON_", "HARBOR_BEACON_TO_GATE_", "HARBOR_EDGE_", "HARBOR_SERVICE_AUTH_",
    "HARBOR_TASK_API_BEARER_TOKEN", "HARBORBEACON_WEB_API_TOKEN", "IM_AGENT_SERVICE_TOKEN",
    "HARBOR_IM_GATEWAY_BEARER_TOKEN", "HARBOR_MODEL_API_TOKEN_",
    "HARBORBEACON_RUNTIME_ENV_FILE", "CREDENTIALS_DIRECTORY",
)


def _assignments(text):
    result = {}
    for item in shlex.split(text, comments=False):
        key, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise HarborMigrationError("Unsupported Harbor environment assignment")
        result[key] = value
    return result


def _unit_environment(text, values, files):
    section = None
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        if section != "[Service]" or line.startswith(("#", ";")):
            continue
        key, _, value = line.partition("=")
        if key == "Environment":
            if not value:
                values.clear()
            else:
                values.update(_assignments(value))
        elif key == "EnvironmentFile":
            if not value:
                files.clear()
            else:
                files.extend(shlex.split(value))


def _file_environment(path):
    values = {}
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise HarborMigrationError("Unsafe Harbor environment file")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise HarborMigrationError("Unsupported Harbor environment file syntax")
            values[key] = " ".join(shlex.split(value, comments=False))
    return values


def _old_environment(migration, unit):
    values, files = {}, []
    relative = "etc/systemd/system/" + unit
    dropins = sorted(key for key in migration.before
                     if key.startswith(relative + ".d/") and key.endswith(".conf"))
    for name in (relative, *dropins):
        if name in migration.before:
            _unit_environment(_safe_path(migration.old_root, name).read_text(encoding="utf-8"), values, files)
    for filename in files:
        name = filename.lstrip("-").lstrip("/")
        if name not in ENV_FILES:
            raise HarborMigrationError("Unreviewed Harbor EnvironmentFile during native migration")
        values.update(_file_environment(_safe_path(migration.old_root, name)))
    return values


def snapshot_native_defaults(root):
    result = {"units": {}, "environment": {}, "gate_state": inventory(root, (GATE_ROOT,))}
    for unit in CORE:
        name = next((f"{directory}/{unit}" for directory in
                     ("etc/systemd/system", "usr/lib/systemd/system")
                     if _safe_path(root, f"{directory}/{unit}").is_file()), None)
        if name is None or _safe_path(root, name).is_symlink():
            raise HarborMigrationError("Native Nexus package unit is missing")
        content = _safe_path(root, name).read_bytes()
        binary = "/usr/bin/" + unit.removesuffix(".service")
        if not re.search(r"(?m)^ExecStart=" + re.escape(binary) + r"(?:\s|$)", content.decode()):
            raise HarborMigrationError("Native Nexus unit does not execute its packaged binary")
        values, files = {}, []
        _unit_environment(content.decode(), values, files)
        expected_profile = ("HARBOR_PRODUCT_PROFILE", "nexus-amd64") if unit == CORE[0] else (
            "HARBORGATE_RUNTIME_PROFILE", "nexus")
        if values.get(expected_profile[0]) != expected_profile[1]:
            raise HarborMigrationError("Native Nexus unit has an unexpected product profile")
        defaults = {}
        for filename in files:
            name_env = filename.lstrip("-").lstrip("/")
            if name_env not in ENV_FILES:
                raise HarborMigrationError("Unreviewed native package environment file")
            defaults.update(_file_environment(_safe_path(root, name_env)))
        result["units"][unit] = (name, content, inventory(root, (name,))[name])
        result["environment"][unit] = (defaults, values)
    return result


def install_native_configuration(migration, defaults, mounts):
    root, expected = migration.new_root, migration.target_overrides
    validate_mounts(root, migration.new_dataset, (BACKUP, GATE_ROOT), mounts)
    backup = _safe_path(root, BACKUP)
    if os.path.lexists(backup):
        raise HarborMigrationError("Native migration backup already exists")
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup.parent.chmod(0o700)
    backup.mkdir(mode=0o700)
    expected[BACKUP] = Entry("dir", 0o700, 0, 0)
    removed = []
    external_paths = {}

    def external_requirement(value, origin):
        if (isinstance(value, str) and value.startswith("/mnt/")
                and not any(value == "/" + path or value.startswith("/" + path + "/") for path in BE_MNT_PATHS)):
            external_paths.setdefault(value, set()).add(origin)

    def parents(relative):
        path = _safe_path(root, relative)
        missing = []
        for parent in path.parents:
            if parent == root or parent.exists():
                break
            missing.append(parent)
        for parent in reversed(missing):
            parent.mkdir(mode=0o700)
            expected[str(parent.relative_to(root))] = Entry("dir", 0o700, 0, 0)
        return path

    selected = [*ENV_FILES, NGINX_HELPER, NGINX_DROPIN]
    for unit in CORE + RETIRED:
        selected.extend(("etc/systemd/system/" + unit, "etc/systemd/system/" + unit + ".d",
                         "etc/systemd/system/multi-user.target.wants/" + unit))
    for relative in selected:
        if relative not in migration.before:
            continue
        archived = BACKUP + "/" + relative
        shutil.move(str(_safe_path(root, relative)), str(parents(archived)))
        removed.append(relative)
        for key, entry in migration.before.items():
            if key == relative or key.startswith(relative + "/"):
                # Cross-dataset shutil.move uses copy2, which drops numeric ownership.
                path = _safe_path(root, BACKUP + "/" + key)
                os.lchown(path, entry.uid, entry.gid)
                if entry.kind != "link":
                    path.chmod(entry.mode)
                expected[BACKUP + "/" + key] = entry
                if entry.kind == "link":
                    migration.allowed_links[BACKUP + "/" + key] = (entry.content,)

    def write(relative, content, entry):
        path = parents(relative)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise HarborMigrationError("Unexpected native configuration destination")
        path.write_bytes(content)
        os.chown(path, entry.uid, entry.gid)
        path.chmod(entry.mode)
        expected[relative] = entry

    for unit, (relative, content, entry) in defaults["units"].items():
        write(relative, content, entry)
        link = "etc/systemd/system/multi-user.target.wants/" + unit
        path = parents(link)
        if os.path.lexists(path):
            raise HarborMigrationError("Unexpected native service enablement")
        target = "/" + relative
        path.symlink_to(target)
        migration.allowed_links[link] = tuple(dict.fromkeys((*migration.allowed_links.get(link, ()), target)))
        expected[link] = Entry("link", 0o777, 0, 0, target)

        fresh, fixed = defaults["environment"][unit]
        old = _old_environment(migration, unit)
        allowed = ("HARBOR_", "HARBORGATE_BASE_URL", "HARBORBEACON_") if unit == CORE[0] else (
            "FEISHU_", "WEIXIN_", "HARBORCLOUD_", "HARBOR_WORKSPACE_ID", "IM_AGENT_PUBLIC_ORIGIN")
        retained = {key: value for key, value in old.items()
                    if key.startswith(allowed) and not key.startswith(AUTH_PREFIXES)}
        if unit == CORE[0]:
            for key, value in retained.items():
                if not any(word in key for word in ("TOKEN", "SECRET", "CREDENTIAL")):
                    external_requirement(value, "beacon_environment:" + key)
        values = {**fresh, **retained, **fixed}
        if unit == CORE[0]:
            values.update(HARBORGATE_BASE_URL="http://127.0.0.1:8787",
                          HARBOR_GATE_MEDIA_ORIGIN="http://127.0.0.1:8787",
                          HARBOR_MODEL_API_BASE_URL="http://127.0.0.1:4174/api/inference/v1",
                          HARBOR_SEMANTIC_ROUTER_TOPOLOGY="embedded", HARBOR_SEMANTIC_ROUTER_LOCAL_ONLY="1")
        else:
            # Config.rs owns exactly these writable paths. Media paths are
            # derived from state_dir; FEISHU_WEBHOOK_PATH is an HTTP route.
            for key in retained:
                if (key.endswith(("_DIR", "_FILE", "_PATH"))
                        and key not in GATE_WRITABLE_PATHS and key != "FEISHU_WEBHOOK_PATH"):
                    raise HarborMigrationError("Unreviewed Gate writable environment path")
            values.update(GATE_WRITABLE_PATHS)
            migration.environment_path_relocations = {
                key: {"source": old.get(key), "target": value}
                for key, value in GATE_WRITABLE_PATHS.items()
            }
            values.update(HARBORBEACON_TURN_ENDPOINT="/api/web/turns", HARBORGATE_FFMPEG_BIN="/usr/bin/ffmpeg")
            if not values.get("HARBOR_WORKSPACE_ID"):
                edge = _file_environment(_safe_path(migration.old_root, "etc/default/harbor-gate-webui-edge"))
                if edge.get("HARBOR_EDGE_WORKSPACE_ID"):
                    values["HARBOR_WORKSPACE_ID"] = edge["HARBOR_EDGE_WORKSPACE_ID"]
        if any("\n" in value or "\r" in value or "\0" in value for value in values.values()):
            raise HarborMigrationError("Unsupported multiline Harbor environment value")
        content = "".join(key + "=" + json.dumps(value, ensure_ascii=False) + "\n"
                          for key, value in sorted(values.items())).encode()
        relative = "etc/default/" + unit.removesuffix(".service")
        write(relative, content, Entry("file", 0o600, 0, 0, hashlib.sha256(content).hexdigest()))

    # Gate's package sandbox permits writes only here. Keep /data clones intact
    # for old references and rollback; populate the new canonical paths as well.
    gate_before = {key: entry for key, entry in migration.before.items()
                   if key == GATE_ROOT or key.startswith(GATE_ROOT + "/")}
    gate_expected = gate_before or defaults["gate_state"]
    expected.update(gate_expected)
    mappings = {"var/lib/harborgate": GATE_ROOT, "data/sessions": GATE_ROOT + "/sessions",
                "data/media-inbound": GATE_ROOT + "/media-inbound",
                "data/media-outbound": GATE_ROOT + "/media-outbound",
                "data/_setup_portal.json": GATE_ROOT + "/_setup_portal.json"}
    for source, destination in mappings.items():
        for name, entry in sorted(migration.before.items(), key=lambda item: (item[0].count("/"), item[0])):
            if name != source and not name.startswith(source + "/"):
                continue
            if entry.kind not in ("dir", "file"):
                raise HarborMigrationError("Unsupported relocated Gate state type")
            relative = destination + name[len(source):]
            path = parents(relative)
            if path.is_symlink():
                raise HarborMigrationError("Symlinked native Gate state destination")
            if entry.kind == "dir":
                path.mkdir(mode=0o700, exist_ok=True)
            else:
                if path.exists() and (not path.is_file() or _digest(path) != entry.content):
                    raise HarborMigrationError("Conflicting legacy and native Gate state")
                shutil.copyfile(_safe_path(migration.old_root, name), path)
            expected[relative] = Entry(entry.kind, 0o700 if entry.kind == "dir" else 0o600, 0, 0, entry.content)
            migration.state_relocations[name] = relative
    for name, entry in list(expected.items()):
        if name == GATE_ROOT or name.startswith(GATE_ROOT + "/"):
            path = _safe_path(root, name)
            path.chmod(0o700 if entry.kind == "dir" else 0o600)
            os.chown(path, 0, 0)
            expected[name] = Entry(entry.kind, 0o700 if entry.kind == "dir" else 0o600, 0, 0, entry.content)

    # The Demo ran Beacon as root; the native package runs as harborbeacon.
    # Change ownership only on copied new-BE trees, never on imported data pools.
    passwd = _safe_path(root, "etc/passwd").read_text().splitlines()
    accounts = [line.split(":") for line in passwd if line.startswith("harborbeacon:")]
    if len(accounts) != 1:
        raise HarborMigrationError("Native Beacon system account is missing")
    uid, gid = int(accounts[0][2]), int(accounts[0][3])
    for name, entry in migration.before.items():
        if any(name == path or name.startswith(path + "/") for path in
               ("var/lib/harboros-beacon", BE_MNT_PATHS[0])):
            os.lchown(_safe_path(root, name), uid, gid)
            expected[name] = Entry(entry.kind, entry.mode, uid, gid, entry.content)
    state_file = _safe_path(root, "var/lib/harboros-beacon/admin-console.json")
    if state_file.is_file():
        def configured_paths(value, origin):
            if isinstance(value, dict):
                for key, child in value.items():
                    if not any(word in key.lower() for word in ("token", "secret", "credential")):
                        configured_paths(child, origin + "." + key)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    configured_paths(child, origin + f"[{index}]")
            else:
                external_requirement(value, origin)
        configured_paths(json.loads(state_file.read_text(encoding="utf-8")), "beacon_state")
    migration.external_access_requirements = [
        {"path": path, "origins": sorted(origins), "account": "harborbeacon", "uid": uid, "gid": gid,
         "status": "pending_target_permission_migration", "external_permissions_modified": False}
        for path, origins in sorted(external_paths.items())
    ]
    migration.target_removed = tuple(removed)
    migration.extra_target_paths = (BACKUP, GATE_ROOT, *(value[0] for value in defaults["units"].values()))
    migration.compatibility_mode = NATIVE_MODE
