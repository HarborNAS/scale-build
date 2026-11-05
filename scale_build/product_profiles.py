"""Formal HarborOS product/architecture profiles for scale-build."""
from __future__ import annotations
import os
from pathlib import Path
import yaml
from .exceptions import CallError

PROFILE_FILE = Path(__file__).resolve().parents[1] / "conf" / "harboros-products.yaml"

def load_profiles() -> dict:
    with PROFILE_FILE.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}

def selected_product() -> str:
    return os.environ.get("HARBOROS_PRODUCT", "nexus").strip().lower()

def selected_architecture() -> str:
    return os.environ.get("HARBOROS_ARCHITECTURE", "amd64").strip().lower()

def selected_profile() -> dict:
    product = selected_product()
    profiles = load_profiles().get("profiles", {})
    profile = profiles.get(product)
    if not isinstance(profile, dict):
        raise CallError(f"Unknown HarborOS product profile: {product}")
    if profile.get("scale_build_supported") is False:
        raise CallError(f"Product {product!r} is built by {profile.get('external_builder')}, not scale-build")
    expected = str(profile.get("architecture", "")).lower()
    actual = selected_architecture()
    if expected != actual:
        raise CallError(f"Product {product!r} requires architecture {expected}, got {actual}")
    return profile

def component_version(package: str, fallback: str) -> str:
    env_name = "HARBOROS_" + package.upper().replace("-", "_") + "_VERSION"
    value = os.environ.get(env_name, "").strip()
    return value or fallback

def external_package_url(package: str, config: dict) -> str:
    profile = selected_profile()
    if package not in {profile.get("beacon_package"), profile.get("gate_package")}:
        return str(config["url_template"]).format(package=package, version=config["deb_version"], arch=selected_architecture())
    version = component_version("beacon" if package == profile.get("beacon_package") else "gate", str(config["deb_version"]))
    if version == "1.0.0" and os.environ.get("HARBOROS_PRODUCT"):
        raise CallError(f"{package} must use an immutable product version; 1.0.0 is not accepted for an explicit product build")
    return str(profile["component_release_url"]).format(package=package, version=version)
