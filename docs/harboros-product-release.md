# HarborOS Nexus / N2 发布整合

## ISO 构建入口

日常操作和 strict candidate 构建使用同一套 `scale-build`，但入口不同：

- **开发测试镜像**：`build test iso`。输入 WebUI 和 middleware 分支，当前默认值为 `develop`；测试 train 为 `-Nightlies`、`-RC` 或 `-BETA`，可选择是否更新测试 OTA 索引。版本由 runner 生成时间戳。
- **正式基础 ISO**：`build iso`。日常输入只有 `version=26.04.0`；workflow 校验 WebUI 和 middleware 存在同名 `26.04.0` 分支，生成 `26.04.0-YYYYMMDD-HHMMSS`，上传 stable ISO/update，但不更新 OTA 索引。
- **strict candidate**：通过 workflow 的可选 strict 输入提供 reviewed immutable manifest。它锁定完整源码 SHA、包 SHA256、包架构、source epoch、组件契约和发布门禁，供 Harbor Assistant 正式候选与审计构建使用。
- **正式 OTA**：新装、升级、重启和回退验收通过后，单独运行 `Release stable OTA update`，输入完整时间戳版本。

当前 WebUI 和 middleware 没有 `master` 分支，开发默认使用 `develop`。正式 `26.04.0` 分支必须先包含已审核的 Harbor Assistant WebUI/middleware 改动；旧基线分支不能直接作为 Harbor Assistant 正式候选。

兼容构建不会猜测 Beacon、Gate 或 HarborLink 的新下载 URL，也不会把普通分支构建宣称为 Harbor Assistant 正式候选。严格候选仍使用已审核的组件包来源和 release manifest。

## 产品与构建边界

| 产品 | 架构与基线 | 组件版本 | 构建入口 |
|---|---|---|---|
| Nexus | amd64，Halfmoon 26.04.0 | `1.1.0+nexus.amd64.<build>` | 本仓库 `make checkout/packages/update/iso` |
| N2 | riscv64，HarborNavi 锁定 Bianbu 4.0.6 输入 | `0.2.0+n2.riscv64.<build>` | HarborNavi `make n2-406-build` |

Nexus 的 Beacon 产品配置使用现有内部 `n1` ABI；N2 保持 `n2`。两条产品线不能交换 Debian 包、rootfs 或安装镜像。

## Strict Nexus 发布输入

strict 模式的 `HARBOROS_RELEASE_MANIFEST` 必须包含 `schema_version=1`、`product=nexus`、`architecture=amd64`、完整组件 Git SHA、包版本/架构/SHA256、真实 artifact URL、runtime 依赖和正整数 `source_date_epoch`。下载后先验证全部 SHA256、Debian 字段和嵌入式组件契约，再以一次本地 APT 事务装入 rootfs。

strict 构建会拒绝工作树脏改动、错误 source pin、错误 epoch、设备凭据和缺失包收据。`assert_publishable` 仍要求所有组件具备资格，并通过新装、升级、boot environment 回退和重启验收。普通开发/分支构建不要求 manifest，也不获得这些正式候选资格。

私有草稿 release 资产通过官方 GitHub API 读取，令牌只存在构建环境；草稿 tag 查询返回 404 时仅分页读取官方 release 列表，401、403 和服务器错误不会回退。

## 手动构建顺序

```text
make checkout
make packages
make update
make iso
```

`make iso` 依赖 `make update`。每次 workflow 都保存 `tmp/release/`、SHA256 和 `logs/GITMANIFEST`。正式 workflow 上传 stable 对象，但不更新 OTA 索引；测试 workflow 上传 test 对象，并按 `releaseUpdate` 选择是否更新测试索引。

## 正式组件准备

正式构建 `version=26.04.0` 前：

1. 审核 WebUI PR #22 和 middleware PR #5。
2. 将已审核提交纳入各自 `26.04.0` 分支，或建立内容完全相同的正式版本分支。
3. 验证分支包含 Harbor Assistant 改动，再运行正式基础 ISO。
4. Harbor Assistant strict candidate 仍使用完整 SHA 和包锁，不依赖可移动分支单独证明内容。

## 首次启动、候选和放行

正式 WebUI 安装于 `/usr/share/truenas/webui`，服务认证、首次启动凭据和 Gate/Beacon/HarborLink 状态按正式包流程处理。包不携带客户照片、会话、索引、模型权重或设备凭据。

当前 Beacon `.6`、Gate、HarborLink 和 WebUI/middleware 候选仍是内部候选；Nexus 分发授权、旧 FFmpeg 溯源材料和真实整机新装/升级/重启/回退验收未完成。N2 继续走独立 RISC-V 流程，未宣称已有 RISC-V 包或实板资格。

不自动合并 Draft PR、不触发 ISO、不更新 stable OTA、不覆盖 GPU/iSCSI dirty changes。


## Daily ISO workflow compatibility

The GitHub Actions pages expose a branch-driven compatibility path for routine
builds. `build test iso` accepts `webui` and `middleware` branch names (the
current forks default to `develop`), a test train suffix and an optional test
OTA-index update. `build iso` accepts the base version `26.04.0`, checks that
both `HarborNAS/webui` and `HarborNAS/middleware` have that branch, and creates
the timestamped system version. It uploads the stable ISO/update files but does
not update the OTA index.

These compatibility builds do not prove that the Harbor Assistant candidate is
inside the image. The candidate path is the optional strict mode with a reviewed
release manifest; it resolves the final component SHAs and package hashes and
runs the publication gates. Before using the simple `version=26.04.0` flow for
Harbor Assistant, the reviewed WebUI and middleware changes must be present on
their `26.04.0` branches. The current repositories do not expose a `master`
branch, so `develop` is the safe development default.
