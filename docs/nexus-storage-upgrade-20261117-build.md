# Nexus Storage native-upgrade candidate

This is an internal native platform/Storage upgrade candidate that retains the
already accepted Demo runtime. It is not a self-contained release of the latest
Beacon/Gate/UI, and fresh-install qualification is outside this candidate's scope.
Migration mechanics and rollback behavior are documented separately in
[harbor-state-migration.md](harbor-state-migration.md).

## Frozen build input

- Lock: `releases/nexus-storage-upgrade-20261117.candidate.json`.
- UTC input time: `2026-11-17T11:20:51Z`.
- System version: `26.04.0-20261117-112051`.
- `source_date_epoch`: `1794914451`.
- Middleware: `7951ea0939f0aac21ef9d755bbc3347fe6a78747`, ref `26.04.0`.
- Base WebUI and all four external packages retain the real Nov 5.2 inputs,
  eligibility flags and provenance gaps. They do not acquire the Demo PDF
  capabilities by being copied into this lock.
- Candidate file SHA256:
  `a70fd6c78dcbbe1a5d4544c37952ca4d93268d34088c1d96ad7d777467fa236e`.
- Canonical lock SHA256 used by `build-receipt.json`:
  `093b454340866a1496691577611406553dd7486f16f7513b526a431c06e56c5c`.

The canonical digest hashes UTF-8 `json.dumps(lock, sort_keys=True,
separators=(',', ':'))`; it is deliberately different from the file digest.
Do not regenerate the timestamp after starting package builds: source receipts
bind both the system version and epoch.

There is no scale-build self-reference field in the existing release-lock
schema. `make checkout` records the actual scale-build HEAD in
`logs/GITMANIFEST`; the generated rootfs receipt currently binds WebUI,
middleware, installed packages and the canonical lock, but not scale-build HEAD.
Record the observed package-builder and final image-builder commits in the
separate internal receipt below. Do not insert the eventual containing commit
into a tracked file that would change that same commit.

## Retained runtime OS dependencies

The Nov 19 read-only process-mapping probe identified the active llama/whisper
Vulkan loader as `libvulkan1:amd64` and the GPU drivers as
`mesa-vulkan-drivers:amd64`. Both are explicit additional rootfs packages, with
recommendations disabled; their mandatory dependencies remain installed by APT.
The four frozen component packages do not declare these runtime dependencies.
`python3-aiohttp` is already a runtime dependency of the pinned `middlewared`
package, and the observed edge loads it from `/usr/lib/python3/dist-packages`.

No observed mapped library came from `/opt` or `/usr/local`; all mapped `/usr`
libraries had package ownership, and the unowned runtime libraries were under
the retained Demo data-pool directory. This observation does not qualify dormant
or not-yet-loaded optional code. Before installation, verify loader resolution
and GPU-driver availability against the assembled candidate rootfs.

These additions change the image manifest hash, not the frozen lock/epoch,
source definitions or the five source-build receipt targets. Preserve the source
relay's original manifest hash and separately record the final image manifest
hash; verify that their source definitions match instead of relabeling the old
relay inventory.

## Artifact-only build

Use an isolated, owned Linux checkout/build directory with the usual root/chroot
capabilities and Debian build prerequisites. Do not dispatch the existing ISO
workflows: their upload behavior is outside this task. These Make targets build
local files; they do not publish artifacts or change an OTA index.

Set these variables in the actual root/container process that runs Make. A
read-only bind mount for the cache must be visible at the same absolute path.
Do not pass a GitHub component OAuth token into the builder.

```sh
export BUILDER_DIR="$PWD"
export HARBOROS_PRODUCT=nexus
export HARBOROS_ARCHITECTURE=amd64
export HARBOROS_STRICT_RELEASE=1
export HARBOROS_RELEASE_MANIFEST="$PWD/releases/nexus-storage-upgrade-20261117.candidate.json"
export SOURCE_DATE_EPOCH=1794914451
export TRUENAS_VERSION=26.04.0-20261117-112051
export HARBOROS_COMPONENT_CACHE=/absolute/read-only/component-cache
unset HARBOROS_COMPONENT_READ_TOKEN

make validate_manifest
make checkout
make packages PACKAGES='truenas_webui truenas middlewared middlewared-docs truenas_files'
```

These five explicit targets are the minimum complete set required by
`locked_build_targets()`. Package dependency discovery adds the required local
build dependencies; in particular the `truenas` metapackage also considers its
installation dependencies. The resulting build can be substantially larger than
five targets. `checkout` has no target selector and all configured source trees
must exist. Do not reduce this command to only `middlewared`: `make update`
requires receipts for all five targets.

Package work may start from `a7a09b2538e2d58d9a8238170bec3f031ac1dea4`
with this candidate supplied as a separately hashed input. Preserve the original
`logs/GITMANIFEST` and the actual package-builder commit/status as evidence.
Before image assembly, install the reviewed, committed migration/nginx source
changes, record the final HEAD, and confirm that the lock, source definitions and package
build logic are unchanged. If package-affecting inputs change, rebuild the
affected packages rather than silently reusing their receipts. A different
scale-build HEAD requires the corresponding Make-managed Python environment.

From that final clean image-builder source, using the same variables and package
directory:

```sh
make update
# Optional installation-media artifact, not needed for native upgrade:
# make iso
```

Do not rerun checkout merely to change the scale-build line in GITMANIFEST: retain
the original package source record and separately record the final image source.
The update embeds `truenas_install` from the final source directory. The four
external cached packages undergo the same SHA256 and component-contract checks
as downloads; a missing cache entry retains the original network behavior, while
a symlink, invalid hash or invalid contract fails the build. Other public packages
and APT inputs still use their normal network sources.

Quick lock validation, inside the build Python environment:

```sh
python - <<'PY'
import json, os
from pathlib import Path
from scale_build.product_profiles import validate_release_lock, apply_release_lock
from scale_build.build_receipts import locked_build_targets
import yaml
lock = validate_release_lock(json.loads(Path(os.environ['HARBOROS_RELEASE_MANIFEST']).read_text()))
manifest = apply_release_lock(yaml.safe_load(Path('conf/build.manifest').read_text()))
assert lock['system_version'] == '26.04.0-20261117-112051'
assert lock['source_date_epoch'] == 1794914451
assert set(locked_build_targets(manifest)) == {
    'truenas_webui', 'truenas', 'middlewared', 'middlewared-docs', 'truenas_files'
}
print('candidate inputs valid')
PY
```

## Existing component evidence

The files below were read and hashed again on Nov 17. Their common local evidence
root is `E:/HarborReleaseIntegration/20261105/`. The candidate keeps their original
audited HTTPS URLs; cache filenames must exactly match those URL basenames.

| Directory and package | Bytes | SHA256 |
|---|---:|---|
| `beacon-artifacts/harboros-beacon_1.1.0+nexus.amd64.20261105.6_linux_amd64.deb` | 15631612 | `b60643e576665cae4668b9f601267289ef5ad848f003f3e15ec6b01043e87d74` |
| `gate-artifacts/harboros-im-gate_1.1.0+nexus.amd64.a0a6a5c_linux_amd64.deb` | 3509920 | `ccd19372d66d3c1560bc3d0e152b107e004f4a8b08f9a139cc100b679c75f8ba` |
| `link-artifacts/harborlink_1.1.0+nexus.amd64.2fdbb27_linux_amd64.deb` | 23689312 | `ea7b25f01628c1eb64501fc99d8e9e15c43275743008507f3ddc788667f711bf` |
| `runtime-artifacts/harboros-ffmpeg_1.0.0_linux_amd64.deb` | 155793662 | `a99decf42b000bfd9c5e54392a4cc6c11a9333fb01ffe6601531671f9442e6a4` |

The SHA256 of each directory's original `artifact-manifest.json` is:

| Directory | Manifest SHA256 |
|---|---|
| `beacon-artifacts` | `b084b3fad241bf0358c8875b9301de7b148b6e7fe0c28a36af2d4a91d912232a` |
| `gate-artifacts` | `287d01cd70ddb810cc50c1fe85726f6d2330b2fcaed23b57877c5ed779dba193` |
| `link-artifacts` | `e35590b37c8eb2824f4cef26ec6ec377b91cac61be8313696a3aeb8b95e0d1da` |
| `runtime-artifacts` | `83816bb3c8e389b686c6ee7d6201d2c334858567d3438049688ca5192ae1d521` |

The FFmpeg manifest explicitly records missing producer source/SBOM/provenance
and license evidence. A byte-identical cache copy does not fill those gaps.
`formal-component-cache.tar` has SHA256
`13b9d13e0ff2a109584dc0e0ad5970061ad0b7e7461a5eef43456566c776f211`;
retain its adjacent file inventory and verify every extracted regular file.

## Separate internal receipt and acceptance

Create a private, checksum-bound JSON receipt after the final source/build hashes
are known. Pending values must remain null or pending, never inferred as passed.
It must bind these independently checkable fields:

- `schema_version: 1`, `scope: native-platform-upgrade-with-retained-runtime`,
  `self_contained_application_image: false`, `fresh_install_acceptance: not_in_scope`.
- `platform_image`: system version, epoch, candidate file and canonical hashes,
  actual package-builder/image-builder full commits, source status, original
  GITMANIFEST hash, update filename/bytes/SHA256, exported build-receipt hash, and
  the exact embedded installer/migration/nginx helper file hashes.
- `base_packages`: the unchanged Nov 5 component source/version/package hashes
  and original evidence-manifest hashes above; actual rootfs dpkg inventory and
  all five validated source-build receipts. Retain actual other source and APT
  dependency inventories; only WebUI/middleware are immutable source pins here.
- `retained_runtime`: Beacon source
  `dc303348b0d0fe3b253617ac504778343d542a03`, actual executable SHA256
  `2b8c0c1eb2b46ee46ac16a05f5b464bdaa73ed5c1feb2b89459de0fe25dccd23`;
  Gate source `7fe6d6fef3361c13b2eea1e7348056dfedcffa66`, actual executable SHA256
  `325d30af71c2582f5d2be5cc90b7f8c71c1b2ad563065f71fdfda73be8a02adf`;
  pre/post unit and drop-in hashes, normalized active ExecStart arguments,
  `/proc/<pid>/exe` and cmdline verification, and private configuration hashes.
- `retained_ui`: source `f9fa96b34246cc55d4378bc42e8af96df896b53f`, r4 archive
  SHA256 `5eca7279fe865531b03ec5668f3f28488f943ccc64d422299604365682fd594a`,
  and observed pre/post deployed file-tree digest. Do not substitute the separate
  `cd1d88` workspace artifact for the actual deployed r4 archive.
- `migration`: reference the private migration receipt and its digest; do not
  copy customer documents, credentials, model contents or factual answers here.
- `acceptance`: record first upgraded boot, reboot and old-BE rollback separately.
  Each stage must verify actual source/process identity, Storage mixed-pool query
  and page rendering, authenticated Demo UI and blocked unauthenticated routes,
  Feishu delivery and existing photo/PDF index access. Previously accepted Demo
  interactions are the pre-upgrade baseline, not evidence of post-upgrade success.

Verify the exported `build-receipt.json` with its `.sha256` and confirm its
`release_lock_sha256` equals the canonical digest above. Confirm the release
`manifest.json` binds that same receipt digest and the actual update artifact.
Archive evidence before promoting any acceptance field. This input remains
`internal_candidate`; its inherited package blockers and out-of-scope new-install
acceptance intentionally fail the public publication gate. Do not merge PR #1,
upload public release files or update OTA indexes as part of these build steps.
