# Nexus stable promotion materials

Builds and internal draft assets do not grant stable publication. The stable
storage and OTA workflows run the offline `scale_build.promotion` verifier
before their first upload/index mutation. Missing or pending evidence fails
closed; none of the historical candidate locks is changed by this tool.
The OTA workflow validates the fixed `tmp/release` directory that its updater
reads; it cannot be redirected to verify different bytes.

```text
python3 -m scale_build.promotion --release-lock releases/<reviewed-stable-lock>.json \
  --manifest <promotion-manifest>.json --artifacts-dir tmp/release \
  --expected-version <exact-system-version>
```

The JSON schema is `conf/promotion-manifest.schema.json`. Run from the exact
`lineage.tag_commit`; all update/ISO/runtime producer commits must be ancestors
of it. Separate lineage must remain explicit when an installer-only repack
reuses an earlier runtime. The tool verifies every artifact's real bytes,
native receipt/lock identity, actual ISO/update readback, appliance acceptance,
three-vendor physical recognition evidence, NVIDIA material qualification and
FFmpeg source/SBOM/provenance. It does not upload, sign or operate an appliance.

The lock must explicitly use `status: stable`, `scope.external_publication:
true`, qualified component/runtime packages and all four existing appliance
acceptance values `passed`. Its canonical SHA256 is SHA256 of UTF-8 JSON with
sorted keys and separators `(',', ':')`. The promotion document references
evidence by artifact basename, never private paths, credentials or mutable URLs.

Each acceptance evidence JSON contains `kind` (`new_install`, `native_upgrade`,
`boot_environment_rollback`, or `reboot`), `status: passed` and the exact
`system_version`. A maintainer must supply recorded results; the tool cannot
authenticate physical observations merely because they are checksum-bound.

Each GPU evidence JSON records `vendor`, `driver`, `kernel_release`, full PCI
BDF `pci_id`, four-hex `pci_vendor_id`/`pci_device_id`, `discrete: true`, positive
`vram_bytes`, `device_nodes`, `recognized: true`, `hardware_api_enumerated: true`,
`service_user_access: true`, `software_device: false`, and
`reset_or_probe_errors: false`. Vendor IDs are NVIDIA `10de`, Intel `8086`, AMD
`1002`; drivers are respectively `nvidia`, `i915`/`xe`, `amdgpu`. NVIDIA needs
its character device and control node; Intel/AMD need a matching render node.
These results qualify only the recorded hardware combination. They do not
promise model inference stability or qualify software Vulkan as a discrete GPU.

## NVIDIA producer input

The release lock's `drivers.nvidia` uses the same `current`, `source_url`,
`sha256` object as `conf/build.manifest`. The 595.104.02 no-compat32 SHA256 was
read from NVIDIA's exact versioned HTTPS `.sha256sum`; it is not inferred from
a filename. Download verification and the installer's own `--check` run before
installation. The sysext producer retains the Vulkan ICD under `/usr`, checks
all four kernel module vermagics, nonempty versioned GSP firmware, ELF64 amd64
userspace libraries and the ICD target, then emits the checksum-bound
`nvidia.raw.qualification.json` inside the image and beside the release files.
Its hardware/inference flags remain false. Actual recognition requires the
separate physical evidence above. The existing kernel configuration is retained.

## FFmpeg 9.1 producer contract

The producer owner is **HarborBeacon**, in
`.github/workflows/build-ffmpeg-deb.yml` and `debian-ffmpeg`; scale-build is only
the consumer. The legacy 1.0.0 asset has no auditable producer source/SBOM and
cannot become publishable by setting `release_eligible: true`.

The new `runtime.packages.harboros-ffmpeg` pin retains `deb_version`, `sha256`,
`architecture: amd64` and the exact versioned URL, and adds this `materials`
object using real producer outputs (angle-bracket values below are placeholders,
not a publishable input):

```json
{
  "producer": {"repository": "https://github.com/<owner>/HarborBeacon", "commit": "<40hex>"},
  "upstream": {"version": "9.1", "url": "https://ffmpeg.org/releases/ffmpeg-9.1.tar.xz", "sha256": "<64hex>"},
  "libopus": {"version": "<exact-version>", "url": "<official-https-source>", "sha256": "<64hex>"},
  "source_date_epoch": 1795333456,
  "toolchain": {"image": "<builder-image>@sha256:<64hex>"},
  "configure_flags": ["<all-actual-flags>", "--enable-libopus"],
  "artifacts": {
    "sbom": {"filename": "ffmpeg.spdx.json", "sha256": "<64hex>"},
    "provenance": {"filename": "ffmpeg.provenance.json", "sha256": "<64hex>"}
  }
}
```

The Debian package must embed
`usr/share/harboros/component-contracts/harboros-ffmpeg.json` with `package`,
`source_commit` equal to the producer commit, `product_profile: nexus-amd64`,
and truthful release blockers. Existing Debian identity checks bind its package,
version and architecture before any maintainer script runs. New material pins
automatically select these checks; historical internal inputs remain readable.

The SBOM is SPDX-2.3 with CC0-1.0 document metadata and exactly one `ffmpeg` and
one `opus` source package, each with version, source download URL, SHA256 and a
real `licenseDeclared`. Include the complete actual dependency/license inventory
and package copyright/source materials in the producer; these checks do not
replace license review.

The provenance is an in-toto Statement v1, predicate type SLSA provenance v1.
Its subject is the exact versioned Debian filename and package SHA256.
`predicate.buildDefinition.resolvedDependencies` contains the FFmpeg/Opus URLs
with `digest.sha256`, plus the producer repository with `digest.gitCommit`.
`externalParameters` records the exact `source_date_epoch` and `configure_flags`;
`predicate.runDetails.builder.id` equals the locked image digest. Promotion
includes these files as `ffmpeg_sbom` and `ffmpeg_provenance` artifacts.

The NVIDIA or FFmpeg runtime change requires a new runtime image/receipt and
new exact artifact hashes. An installer-only repack must not relabel the old
runtime, old native receipt or old acceptance evidence as these new inputs.
The artifact readback also records the embedded `nvidia.raw` SHA256 and size;
both must equal the producer qualification receipt, so promotion cannot verify
one extension payload and publish an update containing another.
