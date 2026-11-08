# Nexus internal candidate inputs

Current reviewed test-ISO input:
`nexus-1.1.0-20261105.2.candidate.json`.

- Product: `nexus`; architecture: `amd64`.
- System version: `26.04.0-NEXUS-RC1-20261105.2`.
- Beacon package: `1.1.0+nexus.amd64.20261105.6`, source
  `7a450e203052bd701fa01d8b5b3523b08c188f67`.
- Gate and HarborLink packages retain their exact immutable source/version/hash.
- `publishArtifacts=false`; `releaseUpdate=false`.

The previous `nexus-1.1.0-20261105.candidate.json` is retained as historical
candidate evidence. It references the earlier Beacon `.5` package, before the
formal rustfmt gate was fixed. Use the `.2` input for the current test build;
no earlier package bits or release input history were overwritten.

All inputs remain internal candidates. Package distribution review and real
appliance installation, upgrade, reboot and boot environment rollback are
pending. Private draft assets require an appropriately scoped component read
credential. Follow [the release guide](../docs/harboros-product-release.md).
