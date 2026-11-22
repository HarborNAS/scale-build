# Nexus internal candidate inputs

## November 22 native package candidate

Current post-Demo input:
[`nexus-native-post-demo-20261122.candidate.json`](nexus-native-post-demo-20261122.candidate.json).

- Product: `nexus`; architecture: `amd64`; status: `internal_candidate`.
- System version: `26.04.0-20261122-074416`.
- Initial `scale-build` input (historical):
  `9eb43128b4fbba9673bd72600ea7a7331e6d370d`.
- Sealed inner rootfs, native runtime and DevTools producer:
  `70ea1bdb9e1ff73ef18d4e348070663ff7d53c4a`, including
  [PR #5](https://github.com/HarborNAS/scale-build/pull/5) for external package
  installation order and [PR #6](https://github.com/HarborNAS/scale-build/pull/6)
  for the actual extension payloads.
- Repaired outer UPDATE and installer producer:
  `eebc56d30718e38ae75f3814060680596a08f7fc`, including the ownership repair in
  [PR #9](https://github.com/HarborNAS/scale-build/pull/9) and controlled repack in
  [PR #10](https://github.com/HarborNAS/scale-build/pull/10). The repack reuses
  the unchanged `70ea` inner rootfs and native build receipt; it does not rebuild
  packages or the kernel. The original `70ea` outer UPDATE failed installation
  and is superseded by the repaired outer.
- ISO assembler producer: `eebc56d30718e38ae75f3814060680596a08f7fc`.
  Assembly and checksum readback passed. The ISO's actual `TrueNAS.update`
  payload is byte-for-byte identical to the repaired outer UPDATE. The inner
  rootfs and original native build receipt remain unchanged; appliance
  installation, upgrade and boot acceptance are not established by this check.

  - Repaired UPDATE SHA-256:
    `88914465d6aeaf9beb38aa6915eeccd4b2cdedb3d1211ceef34856b8c93de595`.
  - ISO SHA-256:
    `d88f23e10bd140f343ec5ba5fb196627a69e69172719e153aafd04cc5b2fc093`.

  Component source commits, package versions and component artifact hashes
  remain pinned in the candidate input.
- Service migration: `native-packages-v1`, using native component packages and
  explicit service/state migration into the new boot environment. This does
  not make the image a self-contained application image; model weights,
  external endpoint qualification and model-runtime provisioning remain separate.
- `external_publication=false`; `ota_index_update=false`. This candidate does
  not enable stable publication or OTA index updates.

The build receipts record the actual image-assembly source and outcome, the
private transport driver used for external-source relay, the producer adaptations
and the online dependency downloads actually used.

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
