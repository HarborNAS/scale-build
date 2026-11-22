"""Explicit Harbor state migration between different boot environments.

The caller stops writers before entering the installer. This module never starts
or stops services, changes the old BE, or recursively copies /etc or a data pool.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import tempfile


class HarborMigrationError(RuntimeError):
    pass


SERVICES = (
    "harboros-beacon.service", "harboros-im-gate.service", "harbor-gate-webui-edge.service",
    "harboros-featured-photos.service", "harboros-subscreen.service",
    "harbor-demo-asr-igpu.service", "harbor-demo-photo-vlm-7b.service",
    "harbor-demo-qwen38.service", "harbor-demo-tts.service", "harbor-photo-demo-nginx.service",
)
PATH_UNITS = ("harbor-photo-demo-nginx.path",)
STATE_PATHS = (
    "var/lib/harboros-beacon", "var/lib/harboros-im-gate", "var/lib/harbor-services",
)
# This directory is on the old BE's /mnt dataset, not an independently imported pool.
# Copy this reviewed root only; never clone /mnt or include the media pool here.
BE_MNT_PATHS = ("mnt/software/harborbeacon-agent-ci", "mnt/software/harbor-apps/ops")
MODEL_STORE = "mnt/software/harborbeacon-agent-ci/model-store"
HF_REPOSITORIES = {
    MODEL_STORE + "/runtimes/harbor-candle/cache/hub/models--jinaai--jina-embeddings-v2-base-zh":
        ("tokenizer.json", "config.json", "model.safetensors"),
    MODEL_STORE + "/tei-rerank/models--BAAI--bge-reranker-base":
        ("tokenizer.json", "tokenizer_config.json", "config.json", "onnx/model.onnx"),
}
OPS_ROOT = "mnt/software/harbor-apps/ops"
CONFIG_PATHS = (
    "etc/default/harboros-beacon-gate", "etc/default/harbor-gate-webui-edge",
    "etc/default/harboros-beacon", "etc/default/harboros-im-gate",
    "etc/systemd/system/harboros-beacon.service.d",
    "etc/systemd/system/harboros-im-gate.service.d",
    "etc/systemd/system/harboros-subscreen.service.d",
    "etc/udev/rules.d/99-harbor-w7900-runtime-pm.rules",
) + tuple(f"etc/systemd/system/{unit}" for unit in SERVICES + PATH_UNITS)
CLONED_PATHS = (
    "data/sessions", "data/media-inbound", "data/media-outbound", "data/_setup_portal.json", "data/harborbeacon",
)
ENABLEMENT_PATHS = tuple(f"etc/systemd/system/multi-user.target.wants/{unit}" for unit in SERVICES + PATH_UNITS)
ALLOWED_LINKS = {
    f"etc/systemd/system/multi-user.target.wants/{unit}": tuple(
        f"/{base}/{unit}" for base in ("etc/systemd/system", "usr/lib/systemd/system", "lib/systemd/system")
    ) for unit in SERVICES + PATH_UNITS
}
RECEIPT_PATH = "var/lib/harboros-upgrade/state-migration.json"
NGINX_WATCHER = "etc/systemd/system/harbor-photo-demo-nginx.service"
NGINX_HELPER = "usr/local/libexec/harbor-demo-nginx-compat.py"
NGINX_DROPIN = "etc/systemd/system/nginx.service.d/40-harbor-demo-compat.conf"
NATIVE_MODE = "native-packages-v1"
NATIVE_SOURCE_PATHS = (
    "var/lib/harborgate", "etc/harboros/service-auth", NGINX_HELPER, NGINX_DROPIN,
)


@dataclass(frozen=True)
class Entry:
    kind: str
    mode: int
    uid: int
    gid: int
    content: str = ""


def _safe_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in (".", "..", "") for part in relative.split("/")):
        raise HarborMigrationError("Invalid Harbor migration path")
    result = Path(root).joinpath(*path.parts)
    # Never traverse a link in a parent, including a dangling link.
    for parent in result.parents:
        if parent == Path(root):
            break
        if parent.is_symlink():
            raise HarborMigrationError("Symlink in Harbor migration parent")
    return result


def _digest(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise HarborMigrationError("Harbor state changed while being read; keep writers stopped")
    return digest


def _internal_link(root, relative, target):
    """Resolve only reviewed cache/file and app/current links in the virtual BE."""
    expected_kind = None
    resolved = None
    for repository, names in HF_REPOSITORIES.items():
        match = re.fullmatch(re.escape(repository) + r"/snapshots/[0-9a-f]{40}/(.+)", relative)
        if match and match[1] in names:
            prefix = "../../../" if match[1].startswith("onnx/") else "../../"
            if not re.fullmatch(re.escape(prefix) + r"blobs/(?:[0-9a-f]{40}|[0-9a-f]{64})", target):
                raise HarborMigrationError("Unexpected model-cache symlink target")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
            if posixpath.dirname(resolved) != repository + "/blobs":
                raise HarborMigrationError("Model-cache symlink escapes its repository")
            expected_kind = "file"
            break
    if relative == OPS_ROOT + "/current":
        if not re.fullmatch("/" + re.escape(OPS_ROOT) + r"/releases/harborops-[A-Za-z0-9_.-]+", target):
            raise HarborMigrationError("Unexpected app release symlink target")
        resolved, expected_kind = target[1:], "dir"
    if resolved is None:
        return None
    destination = _safe_path(root, resolved)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        raise HarborMigrationError("Broken internal Harbor symlink") from None
    expected_type = stat.S_ISREG if expected_kind == "file" else stat.S_ISDIR
    if not expected_type(info.st_mode):
        raise HarborMigrationError("Internal Harbor symlink target is not a regular file or directory")
    return resolved, expected_kind


def inventory(root, paths, allowed_links=None, metadata_only=()):
    """Return metadata and hashes only, without following links or reading devices."""
    root = Path(root)
    allowed_links = allowed_links or {}
    result = {}
    internal_links = []

    def visit(relative, device=None):
        path = _safe_path(root, relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if device is not None and info.st_dev != device:
            raise HarborMigrationError("Nested mount in Harbor migration tree")
        common = (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if target not in allowed_links.get(relative, ()):
                internal = _internal_link(root, relative, target)
                if internal is None:
                    raise HarborMigrationError("Unreviewed symlink in Harbor migration tree")
                internal_links.append(internal)
            result[relative] = Entry("link", *common, target)
        elif stat.S_ISREG(info.st_mode):
            result[relative] = Entry("file", *common, _digest(path))
        elif stat.S_ISDIR(info.st_mode):
            result[relative] = Entry("dir", *common)
            if relative not in metadata_only:
                for child in sorted(path.iterdir()):
                    visit(f"{relative}/{child.name}", info.st_dev)
        else:
            raise HarborMigrationError("Special file in Harbor migration tree")

    for relative in paths:
        visit(relative)
    for relative in metadata_only:
        visit(relative)
    for target, kind in internal_links:
        if target not in result or result[target].kind != kind:
            raise HarborMigrationError("Internal symlink target is outside the copied Harbor inventory")
    return result


def validate_roots(old_root, new_root):
    old, new = Path(old_root).resolve(), Path(new_root).resolve()
    if old == new or (old != Path(old.anchor) and old in new.parents) or new in old.parents:
        raise HarborMigrationError("Old and new boot environment roots overlap")


def validate_mounts(root, dataset, paths, mounts):
    """Ensure each scope belongs to the declared BE, with no nested mount."""
    if not dataset or "/ROOT/" not in dataset:
        raise HarborMigrationError("Missing boot environment dataset identity")
    root = Path(root)
    mounts = tuple(mounts)
    required = {"": dataset}
    for relative in paths:
        component = "var/lib" if relative.startswith("var/lib/") else relative.split("/")[0]
        required[component] = f"{dataset}/{component}"
    for relative, expected in required.items():
        point = root / relative
        candidates = [mount for mount in mounts if Path(mount.mountpoint) == point]
        if len(candidates) != 1 or candidates[0].fs_type != "zfs" or candidates[0].mount_source != expected:
            raise HarborMigrationError("Harbor state is not on the declared boot environment dataset")
    for relative in paths:
        scope = root / relative
        component = "var/lib" if relative.startswith("var/lib/") else relative.split("/")[0]
        containing = [m for m in mounts if Path(m.mountpoint) == scope or Path(m.mountpoint) in scope.parents]
        nearest = max(containing, key=lambda m: len(Path(m.mountpoint).parts), default=None)
        if nearest is None or Path(nearest.mountpoint) != root / component:
            raise HarborMigrationError("Harbor migration scope is hidden by another mount")
        if any(scope == Path(m.mountpoint) or scope in Path(m.mountpoint).parents for m in mounts):
            raise HarborMigrationError("Unexpected mount inside Harbor migration scope")


def require_quiescent(run):
    for unit in SERVICES + PATH_UNITS:
        output = run(["systemctl", "show", unit, "--property=ActiveState,MainPID,ControlPID"]).stdout
        state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        active = state.get("ActiveState") not in ("inactive", "failed")
        has_process = unit in SERVICES and (state.get("MainPID") != "0" or state.get("ControlPID") != "0")
        if active or has_process:
            raise HarborMigrationError("Stop Harbor services and path activation before upgrading")


class HarborStateMigration:
    def __init__(self, old_root, old_dataset, paths, allowed_links, run, mounts, *, service_migration=None):
        self.old_root = Path(old_root)
        self.old_dataset = old_dataset
        self.paths = tuple(paths)
        self.parents = ("mnt/software",) if any(path in self.paths for path in BE_MNT_PATHS) else ()
        if OPS_ROOT in self.paths:
            self.parents += ("mnt/software/harbor-apps",)
        self.allowed_links = dict(allowed_links)
        self.run = run
        validate_mounts(self.old_root, old_dataset, self.paths + CLONED_PATHS, mounts)
        self.before = inventory(self.old_root, self.paths + CLONED_PATHS, allowed_links, self.parents)
        self.new_root = None
        self.new_dataset = None
        self.target_overrides = {}
        self.extra_target_paths = ()
        self.compatibility_mode = None
        self.service_migration = service_migration
        self.target_removed = ()
        self.state_relocations = {}
        self.external_access_requirements = []

    def copy(self, new_root, new_dataset, mounts):
        validate_roots(self.old_root, new_root)
        if new_dataset == self.old_dataset:
            raise HarborMigrationError("Old and new boot environment datasets are identical")
        self.new_root, self.new_dataset = Path(new_root), new_dataset
        validate_mounts(self.new_root, new_dataset, self.paths + CLONED_PATHS, mounts)
        # Check destination paths before invoking rsync; never write through links.
        inventory(self.new_root, self.paths, self.allowed_links, self.parents)
        native_defaults = None
        if self.service_migration == NATIVE_MODE:
            from .harbor_native import snapshot_native_defaults
            native_defaults = snapshot_native_defaults(self.new_root)
        for relative in self.paths:
            if relative not in self.before:
                # New-image defaults must not enable a service disabled in the old BE.
                if relative in ENABLEMENT_PATHS:
                    destination = _safe_path(self.new_root, relative)
                    if os.path.lexists(destination):
                        if not destination.is_symlink():
                            raise HarborMigrationError("Unexpected service enablement file")
                        destination.unlink()
                continue
            source, destination = _safe_path(self.old_root, relative), _safe_path(self.new_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if self.before[relative].kind == "dir":
                if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                    raise HarborMigrationError("Unexpected Harbor destination type")
                self.run(["rsync", "-aH", "--numeric-ids", "--delete", "--", f"{source}/", f"{destination}/"])
            else:
                if destination.is_dir() and not destination.is_symlink():
                    raise HarborMigrationError("Unexpected Harbor destination type")
                if destination.is_symlink() and self.before[relative].kind != "link":
                    raise HarborMigrationError("Unexpected Harbor destination type")
                self.run(["rsync", "-a", "--numeric-ids", "--", str(source), str(destination)])
        for relative in self.parents:
            path = _safe_path(self.new_root, relative)
            entry = self.before[relative]
            if entry.kind != "dir" or not path.is_dir() or path.is_symlink():
                raise HarborMigrationError("Unexpected Harbor parent directory type")
            if hasattr(os, "chown"):
                os.chown(path, entry.uid, entry.gid)
            path.chmod(entry.mode)
        if native_defaults is not None:
            from .harbor_native import install_native_configuration
            install_native_configuration(self, native_defaults, mounts)
        self.verify(mounts, data_permissions_restricted=False)

    def install_nginx_compatibility(self, mounts):
        """Apply only the reviewed new-BE watcher transformation, preserving its old-BE original."""
        if self.service_migration == NATIVE_MODE:
            return
        if NGINX_WATCHER not in self.before:
            return
        from .harbor_demo_nginx import MODE, migration_files

        source = _safe_path(self.old_root, NGINX_WATCHER)
        files = migration_files(source.read_text(encoding="utf-8"))
        if (set(files) != {NGINX_WATCHER, NGINX_HELPER, NGINX_DROPIN} or
                any(not isinstance(data, bytes) for data in files.values())):
            raise HarborMigrationError("Unexpected nginx compatibility migration plan")
        validate_mounts(self.new_root, self.new_dataset, (NGINX_HELPER, NGINX_DROPIN), mounts)
        for relative, content in files.items():
            destination = _safe_path(self.new_root, relative)
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise HarborMigrationError("Unexpected nginx compatibility destination")
            if relative in (NGINX_HELPER, NGINX_DROPIN) and destination.exists():
                raise HarborMigrationError("Nginx compatibility addition unexpectedly exists in new image")
            previous = self.before.get(relative)
            entry = Entry("file", previous.mode if previous else (0o755 if relative == NGINX_HELPER else 0o644),
                          previous.uid if previous else 0, previous.gid if previous else 0,
                          hashlib.sha256(content).hexdigest())
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".harbor-upgrade-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, entry.mode)
                if hasattr(os, "chown"):
                    os.chown(temporary, entry.uid, entry.gid)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.target_overrides[relative] = entry
        self.extra_target_paths = (NGINX_HELPER, NGINX_DROPIN)
        self.compatibility_mode = MODE
        self.verify(mounts, data_permissions_restricted=False)

    def verify(self, mounts, data_permissions_restricted=True):
        if self.new_root is None:
            raise HarborMigrationError("Harbor migration has not copied state")
        validate_mounts(self.old_root, self.old_dataset, self.paths + CLONED_PATHS, mounts)
        validate_mounts(self.new_root, self.new_dataset, self.paths + CLONED_PATHS + self.extra_target_paths, mounts)
        if self.old_root == Path("/"):
            require_quiescent(self.run)
        current = inventory(self.old_root, self.paths + CLONED_PATHS, self.allowed_links, self.parents)
        if current != self.before:
            raise HarborMigrationError("Source Harbor state changed during upgrade; keep writers stopped")
        expected = dict(self.before)
        for relative in self.target_removed:
            expected = {key: value for key, value in expected.items()
                        if key != relative and not key.startswith(relative + "/")}
        expected.update(self.target_overrides)
        if data_permissions_restricted:
            for relative, entry in expected.items():
                if any(relative == path or relative.startswith(path + "/") for path in CLONED_PATHS):
                    mode = 0o700 if entry.kind == "dir" or entry.mode & 0o111 else 0o600
                    expected[relative] = Entry(entry.kind, mode, entry.uid, entry.gid, entry.content)
        actual = inventory(self.new_root, self.paths + CLONED_PATHS + self.extra_target_paths,
                           self.allowed_links, self.parents)
        if actual != expected:
            raise HarborMigrationError("New boot environment Harbor state does not match the stopped source")
        for relative, entry in actual.items():
            if relative in ENABLEMENT_PATHS and entry.kind == "link":
                # Enablement must refer to a real unit within the new BE, not the running host.
                target = _safe_path(self.new_root, entry.content.lstrip("/"))
                if not target.is_file() or target.is_symlink():
                    raise HarborMigrationError("Enabled Harbor service has no regular unit in the new BE")
        return actual

    def write_receipt(self, mounts):
        actual = self.verify(mounts)
        path = _safe_path(self.new_root, RECEIPT_PATH)
        if path.parent.is_symlink() or os.path.lexists(path):
            raise HarborMigrationError("Unexpected Harbor migration receipt destination")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        receipt = {
            "schema_version": 1, "source_dataset": self.old_dataset, "target_dataset": self.new_dataset,
            "paths": list(self.paths), "cloned_paths": list(CLONED_PATHS),
            "parent_metadata_paths": list(self.parents),
            "transformed_target_paths": list(self.target_overrides),
            "extra_target_paths": list(self.extra_target_paths),
            "compatibility_mode": self.compatibility_mode,
            "removed_target_paths": list(self.target_removed),
            "state_relocations": self.state_relocations,
            "external_access_requirements": self.external_access_requirements,
            "source": {key: asdict(value) for key, value in self.before.items()},
            "target": {key: asdict(value) for key, value in actual.items()},
        }
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())


def prepare_harbor_migration(old_root, old_dataset, run, mounts, *, bootfs=None, service_migration=None):
    if old_root is None:
        return None
    if service_migration not in (None, NATIVE_MODE):
        raise HarborMigrationError("Unsupported Harbor service migration policy")
    extra = NATIVE_SOURCE_PATHS if service_migration == NATIVE_MODE else ()
    paths = tuple(path for path in STATE_PATHS + CONFIG_PATHS + BE_MNT_PATHS + extra
                  if os.path.lexists(_safe_path(old_root, path)))
    state_paths = STATE_PATHS
    if service_migration == NATIVE_MODE:
        state_paths += ("var/lib/harborgate", "data/sessions", "data/_setup_portal.json")
    if not any(os.path.lexists(_safe_path(old_root, path)) for path in state_paths):
        return None
    # Live upgrade only: an offline BE cannot prove the active writers are stopped.
    if Path(old_root).resolve() != Path("/"):
        raise HarborMigrationError("Harbor state migration requires a stopped live source system")
    if bootfs != old_dataset:
        raise HarborMigrationError("Active Harbor boot environment differs from the selected bootfs")
    require_quiescent(run)
    return HarborStateMigration(old_root, old_dataset, paths + ENABLEMENT_PATHS, ALLOWED_LINKS, run, mounts,
                                service_migration=service_migration)
