"""Product and immutable component inputs for the Nexus appliance release lane."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse, unquote
import yaml
from .exceptions import CallError

PROFILE_FILE = Path(os.environ.get("BUILDER_DIR", ".")) / "conf" / "harboros-products.yaml"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
NEXUS_VERSION = re.compile(r"1\.1\.0\+nexus\.amd64\.[0-9A-Za-z][0-9A-Za-z.-]*")
SOURCE_NAMES = {"truenas_webui": "webui", "truenas": "middleware"}
PACKAGES = {"harboros-beacon": "HarborBeacon", "harboros-im-gate": "HarborGate", "harborlink": "HarborLink"}


def selected_profile() -> dict:
    product = os.environ.get("HARBOROS_PRODUCT", "nexus")
    architecture = os.environ.get("HARBOROS_ARCHITECTURE", "amd64")
    profiles = yaml.safe_load(PROFILE_FILE.read_text(encoding="utf-8"))["profiles"]
    if product not in profiles:
        raise CallError(f"Unknown HarborOS product profile: {product}")
    profile = profiles[product]
    if profile.get("scale_build_supported") is False:
        raise CallError(f"Product {product!r} is built by {profile['external_builder']}, not scale-build")
    if architecture != profile["architecture"]:
        raise CallError(f"Product {product!r} requires architecture {profile['architecture']}, got {architecture}")
    return {**profile, "product": product}


def source_date_epoch(lock: dict) -> int:
    epoch = lock.get('source_date_epoch')
    if type(epoch) is not int or epoch <= 0:
        raise CallError('Formal Nexus lock requires a positive integer source_date_epoch')
    supplied = os.environ.get('SOURCE_DATE_EPOCH')
    if supplied is not None and (not supplied.isdigit() or int(supplied) != epoch):
        raise CallError('SOURCE_DATE_EPOCH differs from the immutable release lock')
    return epoch


def release_build_epoch() -> int | None:
    if not os.environ.get('HARBOROS_RELEASE_MANIFEST'):
        return None
    lock = release_lock(required=True)
    return source_date_epoch(lock)


def validate_release_lock(lock: dict) -> dict:
    if not isinstance(lock, dict) or lock.get("schema_version") != 1 or lock.get(
            "product") != "nexus" or lock.get("architecture") != "amd64":
        raise CallError("Expected a Nexus/amd64 release manifest, schema_version 1")
    if not re.fullmatch(r"26\.04\.0-NEXUS-[0-9A-Za-z.-]+", str(lock.get("system_version", ""))):
        raise CallError("Nexus lock requires the immutable Halfmoon system version")
    source_date_epoch(lock)
    components = lock.get("components", {})
    for name in ("beacon", "gate", "webui", "middleware", "harborlink", "runtime"):
        item = components.get(name, {})
        if not SHA.fullmatch(str(item.get("commit", ""))):
            raise CallError(f"{name} requires a full immutable source commit")
        repo = urlparse(str(item.get("repository", "")))
        if (repo.scheme != "https" or repo.hostname != "github.com"
                or repo.username or repo.password or repo.query or repo.fragment):
            raise CallError(f"{name} requires a credential-free GitHub repository")
        if name in ("webui", "middleware") and not str(item.get("ref", "")).strip():
            raise CallError(f"{name} requires the source ref containing the pinned commit")
    packages = lock.get("packages", {})
    for package in PACKAGES:
        item = packages.get(package, {})
        if not DIGEST.fullmatch(str(item.get("sha256", ""))):
            raise CallError(f"{package} requires the exact package SHA256")
        if item.get("architecture") != "amd64":
            raise CallError(f"{package} must be an amd64 package")
        version = str(item.get("version", ""))
        if not NEXUS_VERSION.fullmatch(version):
            raise CallError(f"{package} requires version 1.1.0+nexus.amd64.<build>")
        if not re.fullmatch(r"[0-9][0-9A-Za-z.+~:-]*", version):
            raise CallError(f"Invalid immutable package version for {package}")
        name = {"harboros-beacon": "beacon", "harboros-im-gate": "gate", "harborlink": "harborlink"}[package]
        if item.get("source_commit") != components[name]["commit"]:
            raise CallError(f"{package} artifact source differs from the component lock")
    if "harboros-ffmpeg" not in lock.get("runtime", {}).get("packages", {}):
        raise CallError("Nexus media dependency harboros-ffmpeg requires a locked artifact and review state")
    if lock.get("runtime", {}).get("mode") != "embedded" and not lock.get("runtime", {}).get("packages"):
        raise CallError("External model runtime requires locked runtime packages")
    return lock


def release_lock(required: bool = False) -> dict | None:
    selected_profile()
    value = os.environ.get("HARBOROS_RELEASE_MANIFEST", "")
    if not value:
        if required:
            raise CallError("HARBOROS_RELEASE_MANIFEST is required for a formal Nexus build")
        return None
    try:
        return validate_release_lock(json.loads(Path(value).read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise CallError(f"Invalid HarborOS release manifest: {error}") from error


def apply_release_lock(manifest: dict) -> dict:
    lock = release_lock()
    if lock is None:
        return manifest
    result = copy.deepcopy(manifest)
    for source in result["sources"]:
        if source["name"] in SOURCE_NAMES:
            pin = lock["components"][SOURCE_NAMES[source["name"]]]
            source.update(repo=pin["repository"], branch=pin["ref"])
    external = result.setdefault("external-packages", {})
    for package, repository in PACKAGES.items():
        pin = lock["packages"][package]
        version = pin["version"]
        # The package's audited producer provides its exact URL; never infer a
        # Nexus asset name from a K3-only component release.
        url = pin.get("url")
        parsed = urlparse(str(url or ""))
        if (parsed.scheme != "https" or parsed.hostname not in ("github.com", "update.harboros.ai")
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise CallError(f"{package} requires an audited, credential-free artifact URL")
        expected_filename = f"{package}_{version}_linux_amd64.deb"
        if unquote(parsed.path.rsplit("/", 1)[-1]) != expected_filename:
            raise CallError(f"{package} artifact URL must identify its exact versioned Nexus Debian filename")
        if pin.get("product_profile") != "nexus-amd64":
            raise CallError(f"{package} requires the Nexus package profile")
        external[package] = {
            "deb_version": version,
            "sha256": pin["sha256"],
            "url_template": url,
            "component_pin": dict(pin)}
    for package, pin in lock.get("runtime", {}).get("packages", {}).items():
        if package in PACKAGES:
            raise CallError("Runtime packages cannot replace validated component packages")
        if pin.get("architecture") != "amd64" or not pin.get("deb_version"):
            raise CallError(f"Runtime package {package} requires an amd64 versioned identity")
        source = urlparse(str(pin.get("url_template", "")))
        if (source.scheme != "https" or source.hostname not in ("github.com", "update.harboros.ai")
                or source.username or source.password or source.query or source.fragment):
            raise CallError(f"Runtime package {package} requires an audited artifact URL")
        if not DIGEST.fullmatch(str(pin.get("sha256", ""))):
            raise CallError(f"Runtime package {package} requires SHA256")
        external[package] = dict(pin)
    return result


def verify_source_pin(package: str, actual_commit: str, actual_repository: str) -> None:
    lock = release_lock()
    if lock is None or package not in SOURCE_NAMES:
        return
    pin = lock["components"][SOURCE_NAMES[package]]
    if actual_commit != pin["commit"] or actual_repository.removesuffix(
            ".git") != pin["repository"].removesuffix(".git"):
        raise CallError(f"{package} checkout does not match the immutable release source pin")


def verify_download(path: Path, expected: str | None) -> None:
    if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise CallError(f"Downloaded package failed SHA256 validation: {path.name}")


def verify_unprovisioned_image(root: Path) -> None:
    """Device-specific service credentials must only be provisioned on first boot."""
    auth = root / "etc/harboros/service-auth"
    if auth.is_symlink() or (auth.exists() and any(path.is_file() or path.is_symlink() for path in auth.rglob("*"))):
        raise CallError("Image contains provisioned device service credentials; rebuild from a clean rootfs")
    link_secrets = root / "var/lib/harborlink/secrets"
    if link_secrets.is_symlink() or (link_secrets.exists() and any(path.is_file() or path.is_symlink()
                                                                   for path in link_secrets.rglob("*"))):
        raise CallError("Image contains provisioned HarborLink device credentials")
    for relative in ("etc/default/harboros-beacon", "etc/default/harboros-im-gate", "etc/default/harboros-beacon-gate"):
        path = root / relative
        if path.is_symlink():
            raise CallError("Image credential environment must not be a symlink")
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and (key.strip().endswith("_TOKEN") or key.strip().endswith(
                    "_SECRET")) and value.strip().strip("\"'"):
                raise CallError("Image environment contains a provisioned device credential")


def source_pin(package: str) -> dict | None:
    lock = release_lock()
    return lock['components'][SOURCE_NAMES[package]] if lock and package in SOURCE_NAMES else None


def checkout_pinned_source(path: str, repository: str, commit: str, git_args: list[str]) -> None:
    """Reproduce an older lock even after its discovery branch moves."""
    import subprocess
    source = Path(path)
    if source.exists():
        status = subprocess.check_output([*git_args, '-C', str(source), 'status', '--porcelain'], text=True)
        if status.strip():
            raise CallError("Refusing to overwrite modified pinned build sources")
        origin = subprocess.check_output([*git_args, '-C', str(source), 'remote',
                                         'get-url', 'origin'], text=True).strip()
        if origin.removesuffix('.git') != repository.removesuffix('.git'):
            raise CallError("Pinned source checkout has another repository")
    else:
        subprocess.run([*git_args, 'clone', '--no-checkout', '--filter=blob:none',
                       '--no-tags', repository, str(source)], check=True)
    subprocess.run([*git_args, '-C', str(source), 'fetch', '--depth', '1', 'origin', commit], check=True)
    subprocess.run([*git_args, '-C', str(source), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
    actual = subprocess.check_output([*git_args, '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != commit:
        raise CallError("Fetched source differs from the immutable release commit")


def assert_publishable(lock: dict) -> None:
    items = {**lock['packages'], **lock.get('runtime', {}).get('packages', {})}
    blocked = [name for name, item in items.items() if item.get(
        'release_eligible') is not True or item.get('blocking_reasons')]
    acceptance = lock.get('acceptance', {})
    missing = [
        name for name in (
            'new_install',
            'native_upgrade',
            'boot_environment_rollback',
            'reboot') if acceptance.get(name) != 'passed']
    if blocked or missing:
        raise CallError(
            f"Release is an internal candidate: unqualified packages={blocked}; pending appliance acceptance={missing}")


def github_asset_request(download_url: str, get):
    """Resolve a versioned private/public release asset without persisting the token."""
    from urllib.parse import unquote, quote
    parsed = urlparse(download_url)
    if parsed.hostname != 'github.com':
        return download_url, {}
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 6 or parts[2:4] != ['releases', 'download']:
        raise CallError('Expected a versioned GitHub release asset URL')
    owner, repo, _, _, tag, filename = parts
    token = os.environ.get('HARBOROS_COMPONENT_READ_TOKEN', '')
    headers = {'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    metadata = get(
        'https://api.github.com/repos/' +
        owner +
        '/' +
        repo +
        '/releases/tags/' +
        quote(
            unquote(tag),
            safe=''),
        headers=headers,
        timeout=30)
    if metadata.status_code == 404:
        # Drafts are absent from tag lookup but visible to authenticated list readers.
        page = 1
        release = None
        while release is None:
            listing = get('https://api.github.com/repos/' + owner + '/' + repo
                          + f'/releases?per_page=100&page={page}', headers=headers, timeout=30)
            listing.raise_for_status()
            rows = listing.json()
            if not isinstance(rows, list):
                raise CallError('GitHub returned an invalid release list')
            matches = [item for item in rows if isinstance(item, dict) and item.get('tag_name') == unquote(tag)]
            if len(matches) > 1:
                raise CallError('The locked release tag is ambiguous')
            if matches:
                release = matches[0]
            elif not rows or (len(rows) < 100 and 'rel="next"' not in str(listing.headers.get('Link', ''))):
                raise CallError('The locked release tag is not visible to the authenticated reader')
            else:
                page += 1
    else:
        metadata.raise_for_status()
        release = metadata.json()
    matches = [item for item in release.get('assets', []) if item.get('name') == unquote(filename)]
    if len(matches) != 1:
        raise CallError('The locked release asset is missing or ambiguous')
    asset = matches[0].get('url', '')
    expected = 'https://api.github.com/repos/' + owner + '/' + repo + '/releases/assets/'
    if not asset.startswith(expected) or not asset[len(expected):].isdigit():
        raise CallError('GitHub returned an unexpected asset download origin')
    return asset, {**headers, 'Accept': 'application/octet-stream'}


def verify_debian_component(path: Path, package: str, pin: dict) -> None:
    """Verify Debian fields and its embedded source/profile before any installation."""
    import email.parser
    import subprocess
    import tarfile
    fields = email.parser.Parser().parsestr(subprocess.check_output(
        ['dpkg-deb', '--field', str(path)], text=True))
    for field, expected in (('Package', package), ('Version', pin['version']), ('Architecture', pin['architecture'])):
        if fields.get(field) != expected:
            raise CallError(f'{package} Debian {field} differs from the release lock')
    expected_path = f'usr/share/harboros/component-contracts/{package}.json'
    contracts = []
    with subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(path)], stdout=subprocess.PIPE) as process:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                if member.name.removeprefix('./') == expected_path:
                    if not member.isfile() or member.size > 65536:
                        raise CallError(f'{package} component contract is not a bounded regular file')
                    contracts.append(json.loads(archive.extractfile(member).read()))
        if process.wait() != 0:
            raise CallError(f'{package} Debian payload cannot be read')
    if len(contracts) != 1:
        raise CallError(f'{package} requires one embedded component contract')
    contract = contracts[0]
    if contract.get('package') != package or contract.get(
            'source_commit') != pin['source_commit'] or contract.get('product_profile') != 'nexus-amd64':
        raise CallError(f'{package} embedded source/profile differs from the release lock')
    if pin.get('release_eligible') is True and (contract.get(
            'release_eligible') is False or contract.get('blocking_reasons')):
        raise CallError(f'{package} embedded blockers cannot be overridden by the release lock')
