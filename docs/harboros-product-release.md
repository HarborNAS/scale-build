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
  `system_version=26.04.0-NEXUS-<revision>`，以及正整数 `source_date_epoch`。
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
草稿 release 的按 tag 查询可能返回 404；解析器只在这一状态下分页读取官方 GitHub releases 列表，
按完全相同的 tag_name 找到草稿，再使用同仓库的数字 asset API 下载。401、403 或服务器错误不会回退。
逻辑 URL 仍使用带版本的 release/download 路径，不为草稿伪造可公开访问的标签。
构建机还须具备所锁源码仓库的 Git 读取权限。现有 COS/R2 分发凭据不等于组件读取凭据。
内部候选锁中的 URL 在对应资产上传之前不可用，不应把只有文件名的计划写成可下载产物。

## 手动构建

同事在已配置的 Linux ISO 构建机上使用评审后的锁：

```sh
export HARBOROS_PRODUCT=nexus
export HARBOROS_ARCHITECTURE=amd64
export HARBOROS_RELEASE_MANIFEST="$PWD/releases/<reviewed-nexus-release>.json"
export TRUENAS_VENDOR=HarborOS
export TRUENAS_VERSION="$(python3 -c 'import json,os;print(json.load(open(os.environ["HARBOROS_RELEASE_MANIFEST"]))["system_version"])')"
export SOURCE_DATE_EPOCH="$(python3 -c 'import json,os;print(json.load(open(os.environ["HARBOROS_RELEASE_MANIFEST"]))["source_date_epoch"])')"
export TRUENAS_TRAIN=TrueNAS-SCALE-Halfmoon-RC
make checkout
make packages
make update
make iso
```

四个阶段使用同一锁、系统版本、SOURCE_DATE_EPOCH 和 train。
正式构建会拒绝与锁不符的 epoch 或系统版本；BUILD_TIME 与生成的 Debian 版本时间戳使用锁定的 UTC epoch。
这只固定已声明的时间输入，不意味着完整 ISO 已可重现；APT、编译器和实际安装依赖仍由收据记录并待人工资格验证。Actions 的两个 ISO workflow 仍只有
`workflow_dispatch`，默认保存候选 artifact。`publishArtifacts=false`，测试更新索引的
`releaseUpdate=false`；组件候选或真机验收未通过时，发布开关被资格检查拒绝。
不会自动发布 stable，也不会把 N2 交给本 ISO profile。

完整锁随 rootfs 安装至 `/usr/share/harboros/release-manifest.json`，
Git manifest 使用完整 SHA。不要在工作树脏改动上复用组件源码 checkout。

## 实际构建收据与法律声明

WebUI、middleware 及其共享源码子包在保存实际 Debian 输出之后、删除构建 overlay 之前，
写入 tmp/pkgdir/<build-target>.build-receipt.json 及 SHA256 旁文件。收据绑定源码仓库和完整 SHA、
系统版本、source_date_epoch、实际 .deb/.udeb 字节 SHA256、Debian 名称/版本/架构/依赖字段、
构建 chroot 的实际 dpkg 包与依赖清单，以及限定的 node/yarn/python3/dpkg-deb/gcc 版本。
不序列化环境变量、secret_env、服务 token 或客户数据。版本探测不允许 Corepack 联网。

正式源码工作树脏改动被拒绝。源码不变但 epoch、系统版本或收据不匹配时，缓存包重新构建。
rootfs 组装会重新验证每个锁定源码目标的收据摘要、实际 Debian 字节和字段、源码 pin、
已安装版本/架构，以及全部锁定外部包；缺失或篡改会在 mksquashfs 之前失败。
实际 rootfs dpkg 依赖清单和锁定外部包记录到 /usr/share/harboros/build-receipt.json，
同时输出到候选目录和 update 内层，并由候选 manifest 绑定收据 SHA256。
这属于受信构建过程的输入/输出证据，不替代完整可重现构建或真机验收。

原有清理仍移除其他 /usr/share/doc 内容、APT 缓存和临时列表；只保留以下组件目录内的
许可证与第三方声明：harboros-beacon、harboros-im-gate、harborlink、harboros-ffmpeg。

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
