# Nexus internal candidate inputs

## November 23 strict native candidate

Current post-Demo input:
[`nexus-native-post-demo-20261122.2.candidate.json`](nexus-native-post-demo-20261122.2.candidate.json).

- Product: `nexus`; architecture: `amd64`; status: `internal_candidate`.
- System version: `26.04.0-20261123-041147`; source epoch: `1795407107`.
- Strict producer: `527cb3b207b744c1aea23c271d13b93157b6569f`.
  [Actions run 45123866502](https://github.com/HarborNAS/scale-build/actions/runs/45123866502)
  built the UPDATE and ISO and saved the candidate artifact successfully.
  The artifact is `harboros-nexus-26.04.0-20261123-041147`, ID
  `14795308803`, 5,040,678,245 archive bytes, with archive SHA-256
  `b166d2ce0db8455f686f360519393fc0b843b58084eb30cda659252d2cb0e259`.
  It expires on 2027-02-21 unless retained separately.
- Independent download and readback covered all 14 exported files:

  - UPDATE: `TrueNAS-SCALE-26.04.0-20261123-041147.update`,
    2,278,727,680 bytes, SHA-256
    `173148cfb6f52101f004b73d6310ccaedb163ad1f1bf665d52828f90726e8ab8`.
  - ISO: `TrueNAS-SCALE-26.04.0-20261123-041147-HarborOS.iso`,
    2,681,272,320 bytes, SHA-256
    `351696f8de074502b2a2134ea2e231548b662f88a95e7e0ec222e78d51afef6c`.
  - The ISO's extracted `TrueNAS.update` is byte-for-byte identical to the
    exported UPDATE. All five exported SHA-256 sidecars match the actual
    ISO, UPDATE, build receipt, NVIDIA qualification and rootfs mtree bytes.
- The exported release-lock file SHA-256 is
  `fe7d9b18161905cae8fc2eabf4a1521269d1089cff218b123697fbcbd4df78a0`;
  its canonical JSON SHA-256 is
  `288d09dc0bb4493c76e0543a5427413135b0b6f00857f6f748e9734d88a6c0d5`,
  exactly matching the build receipt. The build receipt SHA-256 is
  `e9d6728f59b1bd8f07ea4a99521b0e55acdba2ef3617a0fd46ac1ce46e8dd676`.
  `GITMANIFEST` records 51 exact source pins, including this producer,
  middleware `ca805fd31cdd6cc4f628581eb2420e57944a630f` and WebUI
  `d7afcd9f80cacdf2411488192e0f51a67f9772d7`.
- NVIDIA 595.104.02 archive and staged payload checks passed. The embedded
  `nvidia.raw` is 435,105,792 bytes with SHA-256
  `ce9d2cdde3bb6881d00368fc484dc07b7b4f3a91c565ceedd3def0cd809f0942`;
  its qualification records `payload_verified=true`, while physical hardware
  recognition and inference qualification remain false as required before
  appliance testing.
- `external_publication=false`; `ota_index_update=false`. The workflow did not
  receive a promotion manifest, so stable validation and stable-storage upload
  were skipped. New installation, native upgrade, reboot, boot-environment
  rollback and three-vendor physical GPU evidence remain pending. The owner
  explicitly skipped the full pre-deployment suite for this internal candidate.

The build fixes needed to reach this sealed candidate are retained in
[PR #15](https://github.com/HarborNAS/scale-build/pull/15),
[PR #16](https://github.com/HarborNAS/scale-build/pull/16),
[PR #17](https://github.com/HarborNAS/scale-build/pull/17) and
[PR #18](https://github.com/HarborNAS/scale-build/pull/18). They keep real APT
and NVIDIA payload failures fatal and do not relax stable promotion gates.

## November 22 superseded native package candidate

Earlier post-Demo input:
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
