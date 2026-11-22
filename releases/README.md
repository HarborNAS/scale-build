# Nexus internal candidate inputs

## November 22 native package candidate

Current post-Demo input:
[`nexus-native-post-demo-20261122.candidate.json`](nexus-native-post-demo-20261122.candidate.json).

- Product: `nexus`; architecture: `amd64`; status: `internal_candidate`.
- System version: `26.04.0-20261122-074416`.
- Initial `scale-build` source pin (before image-assembly fixes):
  `9eb43128b4fbba9673bd72600ea7a7331e6d370d`. Component source commits,
  package versions and artifact hashes are pinned in the candidate input.
  The actual image-assembly source and outcome are recorded in the build
  delivery receipts.
- Service migration: `native-packages-v1`, using native component packages and
  explicit service/state migration into the new boot environment. This does
  not make the image a self-contained application image; model weights,
  external endpoint qualification and model-runtime provisioning remain separate.
- `external_publication=false`; `ota_index_update=false`. This candidate does
  not enable stable publication or OTA index updates.

New installation, native upgrade, reboot and boot environment rollback
acceptance must come from recorded appliance runs. Producing packages, an update
or an ISO does not establish that acceptance or override the component
distribution and runtime qualification gates in the candidate input. This
catalog update does not change the frozen manifest or the pinned build input.

## November 17 platform upgrade candidate

[`nexus-storage-upgrade-20261117.candidate.json`](nexus-storage-upgrade-20261117.candidate.json)
is retained as the historical platform upgrade input for
`26.04.0-20261117-112051`. Its scope retains the existing appliance runtime;
it does not claim a self-contained application image or fresh-install acceptance.

## November 5 reviewed test-ISO inputs

The reviewed November 5 test-ISO input was:
`nexus-1.1.0-20261105.2.candidate.json`.

- Product: `nexus`; architecture: `amd64`.
- System version: `26.04.0-NEXUS-RC1-20261105.2`.
- Beacon package: `1.1.0+nexus.amd64.20261105.6`, source
  `7a450e203052bd701fa01d8b5b3523b08c188f67`.
- Gate and HarborLink packages retain their exact immutable source/version/hash.
- `publishArtifacts=false`; `releaseUpdate=false`.

The previous `nexus-1.1.0-20261105.candidate.json` is retained as historical
candidate evidence. It references the earlier Beacon `.5` package, before the
formal rustfmt gate was fixed. The `.2` input superseded that test input;
no earlier package bits or release input history were overwritten.

All inputs remain internal candidates. Package distribution review and appliance
installation, upgrade, reboot and boot environment rollback remain subject to
their qualification gates and recorded results. Private draft assets require an
appropriately scoped component read credential. Follow
[the release guide](../docs/harboros-product-release.md).
