# HarborOS Nexus / N2 发布整合

## 产品与构建边界

| 产品 | 架构与基线 | 组件版本 | 构建入口 |
|---|---|---|---|
| Nexus | amd64，Halfmoon 26.04.0 | `1.1.0+nexus.amd64.<build>` | 本仓库 `make checkout/packages/update/iso` |
| N2 | riscv64，HarborNavi 锁定 Bianbu 4.0.6 输入 | `0.2.0+n2.riscv64.<build>` | HarborNavi `make n2-406-build` |

Nexus 的 Beacon 产品配置使用现有内部 `n1` ABI；N2 保持 `n2`。
`nexus` 不成为新的 Beacon runtime 枚举。两者共用业务协议和 WebUI 源码，
不能交换 Debian 包、rootfs 或安装镜像。

## Nexus 发布输入

`HARBOROS_RELEASE_MANIFEST` 指向已评审、纳入版本管理的 JSON。输入至少包含：

- `schema_version=1`、`product=nexus`、`architecture=amd64`，以及
  `system_version=26.04.0-NEXUS-<revision>`。
- `components.beacon/gate/webui/middleware/harborlink/runtime` 的 GitHub 仓库和完整 Git SHA。
  WebUI、middleware 额外记录引用名及 26.04.0 基线；实际 checkout 使用 SHA，引用移动不改变输入。
- `packages.harboros-beacon/harboros-im-gate/harborlink` 的 Nexus 版本、amd64、源码 SHA、
  SHA256、`product_profile=nexus-amd64` 和真实、带版本的 artifact URL。
- `runtime.mode` 及 `runtime.packages`。即使 Candle 编译进 Beacon，媒体依赖
  `harboros-ffmpeg` 仍须锁定版本、amd64、SHA256、URL 和资格状态。
- 各包的 `release_eligible`、blocker，以及整机新装、升级、boot environment 回退和重启验收状态。

下载后先验证全部 SHA256 和组件包内的 Debian 名称、版本、架构、源码/产品契约，
再以一次本地 APT 事务装入 rootfs chroot。组件不再回退到无法区分内容的 1.0.0。
包 URL 必须来自实际 producer；不能根据 K3 包名猜测 Nexus 资产名。

私有 GitHub release 资产读取使用构建机的最小只读凭据；Actions 可配置
`COMPONENT_RELEASE_READ_TOKEN`。令牌仅通过环境传给官方 GitHub API，不进入锁、包或日志。
构建机还须具备所锁源码仓库的 Git 读取权限。现有 COS/R2 分发凭据不等于组件读取凭据。
内部候选锁中的 URL 在对应资产上传之前不可用，不应把只有文件名的计划写成可下载产物。

## 手动构建

同事在已配置的 Linux ISO 构建机上使用评审后的锁：

```sh
export HARBOROS_PRODUCT=nexus
export HARBOROS_ARCHITECTURE=amd64
export HARBOROS_RELEASE_MANIFEST="$PWD/releases/<reviewed-nexus-release>.json"
export TRUENAS_VENDOR=HarborOS
export TRUENAS_VERSION=26.04.0-NEXUS-RC1
export TRUENAS_TRAIN=TrueNAS-SCALE-Halfmoon-RC
make checkout
make packages
make update
make iso
```

四个阶段使用同一锁、系统版本和 train。Actions 的两个 ISO workflow 仍只有
`workflow_dispatch`，默认保存候选 artifact。`publishArtifacts=false`，测试更新索引的
`releaseUpdate=false`；组件候选或真机验收未通过时，发布开关被资格检查拒绝。
不会自动发布 stable，也不会把 N2 交给本 ISO profile。

完整锁随 rootfs 安装至 `/usr/share/harboros/release-manifest.json`，
Git manifest 使用完整 SHA。不要在工作树脏改动上复用组件源码 checkout。

## 首次启动和升级

正式 WebUI 安装于 `/usr/share/truenas/webui`，middleware 的同源代理经 Gate 验证
`X-HarborOS-Auth-Token`，再使用每台设备的服务凭据调用 Beacon。
Gate、Beacon 和 HarborLink 安装到各自正式目录，不共享运行状态。

ISO chroot 安装使用 `SYSTEMD_OFFLINE=1`，不生成共享的设备私钥或模型 token。
首次开机按凭据恢复 unit、HarborLink 凭据 unit、业务服务的顺序准备私有凭据；
升级保留已有配置、账号、凭据、索引和模型绑定。rootfs 打包前若发现设备私钥、
旧环境文件中的服务 token 或 HarborLink secrets，会失败并要求使用干净输入。

包不携带客户照片、会话、索引或模型权重。Model Center 下载/注册之后，才可按实际
runtime 健康状态启用能力。Candle/bootstrap 代码和目录保留，不把空模型目录显示为 ready。
可选 OCR、Embedding、VLM、ASR、TTS runtime 未配置时保留真实 blocker。
Demo tar、4176 edge、临时权限、全局 Bearer 例外和 `/var/lib` WebUI 指针不参与新装路径。

## 候选与放行

组件构建/包契约测试只形成内部候选。现有 Beacon/HarborLink 的第一方记录明确限于
HarborNavi qualification，不能由工具改名为 Nexus 发行批准。旧 FFmpeg 1.0.0 的实际
二进制摘要可以锁定，但缺少完整许可证、源码和构建材料时，也不能标为发布已审查。
这些状态和真机结果记录在锁内，不靠修改文案消除 blocker。

同事随后手动执行 test ISO 新装、上一正式版本原生升级、PID1 systemd 重启、
Chat/RAG/照片/飞书/ASR/TTS/模型选择、boot environment 回退和配置持久性检查。
只有通过该组合版本验收，才形成可分发的 `.update` 和 ISO。
N2 另行验证 RISC-V ELF、Python ABI、供应商 base、boot geometry、固定 runtime、
更新与恢复；工程锁和 amd64 编译成功都不能替代 N2 实板验收。
