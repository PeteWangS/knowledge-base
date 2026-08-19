---
created: 2026-08-19
tags: [docker, dockerfile, security, sbom, provenance, scout, buildkit, supply-chain]
source: Docker 官方文档
---

# Docker 镜像优化与构建安全深度（构建密钥 / SBOM / Provenance / Scout / Build policies）

## 概述

本篇为 Docker 主题**镜像优化与安全深度篇**，承接 [[dockerfile-best-practices]]（2026-07-29 基础篇：指令规范、多阶段基础、镜像优化入门）与 [[docker-buildkit-cache-supplychain-security]]（2026-08-11 深度篇：BuildKit 缓存机制、多阶段高级模式、供应链安全），本轮聚焦官方 best-practices 全文细节 + 镜像优化与安全深度链路：

1. **Dockerfile 编写规范细节**：apt-get 缓存策略（update 与 install 同层）、ENV/unset 层不可变陷阱、管道退出码（pipefail）、多行参数排序、复杂 RUN 用 heredoc
2. **构建期安全**：secret/SSH mount 注入密钥（不落层、不进缓存、不进 provenance）、.dockerignore 裁剪上下文、非 root 用户
3. **镜像内容透明**：SBOM 成分清单（SPDX 格式、Syft 扫描器）与 Provenance 构建溯源（SLSA 格式、mode=min/max），以 in-toto Statement 挂 image index
4. **运行前扫描与策略评估**：Docker Scout（cves 漏洞分析 + quickview 六项内置策略）、实验特性 Build policies（OPA Rego 构建前校验）

核心链路：基础镜像选择（可信来源 + 最小化 + pin digest）→ Dockerfile 指令规范（多阶段、层顺序、缓存策略）→ 构建期安全（secret mount、.dockerignore）→ 镜像内容透明（SBOM + Provenance attestation）→ 运行前扫描与策略评估（Scout、Build policies）。

全部素材取自 Docker 官方文档 `docker/docs` 仓库（best-practices / multi-stage / base-images / secrets / cache optimize / attestations / Scout / policies）。

---

## 架构图

### 镜像构建与安全流水线

![[assets/docker/diagram-docker-build-security-arch.svg]]

*图：镜像构建与安全全链路——context 裁剪 → BuildKit 多阶段构建（层缓存）→ attestation 附加（Provenance 默认 + SBOM）→ 推送 registry → Scout 漏洞/策略评估 → 合规发布；Build policies 在构建前用 Rego 拦截不合规输入。*

---

## 核心概念

### 1. 多阶段构建（Multi-stage builds）

一个 Dockerfile 多个 FROM 指令，每 FROM 开启新阶段，可 `COPY --from=<name>` 选择性复制产物，最终镜像只含运行所需文件。BuildKit 只构建目标阶段依赖的阶段（legacy builder 会全量构建）。

> **关键点**：`FROM ... AS <name>` 命名阶段 + `COPY --from=name`，避免数字索引；`docker build --target <stage>` 可停在指定阶段（debug 与 production 双阶段）；`COPY --from=nginx:latest` 可直接从外部镜像复制。

### 2. 基础镜像选择（Base image）

首选带徽章的镜像：**Docker Official Images**（策展、有文档、定期更新）、**Verified Publisher**（官方组织维护）、**DSOS**（Docker 赞助开源）；选最小且满足需求的基础镜像以减少漏洞面；构建/测试用完整镜像、生产用更瘦镜像。

> **关键点**：推荐 Alpine（<6MB 完整发行版）；`FROM alpine:3.21` 的 tag 会漂移，供应链完整性可 pin digest（`FROM alpine:3.21@sha256:...`）；Scout Up-to-Date Base Images 策略可检测 pin 过期并自动发 remediation PR。

### 3. 构建缓存与层顺序（Build cache & layer ordering）

每条指令生成可缓存层，指令或依赖文件变化则后续层失效重建；昂贵且少变的步骤放前面，频繁变化的源码放最后；.dockerignore 排除无关文件缩小上下文。

> **关键点**：先 `COPY package.json` + `RUN npm install`，再 `COPY . .`；apt-get 必须 `RUN apt-get update && apt-get install` 同一层（cache busting），可版本 pin（`s3cmd=1.1.*`）强制刷新。

### 4. 缓存挂载与绑定挂载（Cache & bind mounts）

`--mount=type=cache,target=...` 提供跨构建持久缓存（累积读写），层重建时只下载新增包；`--mount=type=bind` 临时挂载源码仅供单条 RUN 使用，不落层不落缓存。

> **关键点**：Go: `/go/pkg/mod` + `/root/.cache/go-build`；npm: `/root/.npm`；pip: `/root/.cache/pip`；apt 需 `sharing=locked`（独占访问）；bind mount 默认只读，构建产物必须写到挂载点之外。

### 5. 构建密钥（Build secrets）

secret mount 把密钥临时挂进构建容器（默认 `/run/secrets/<id>`），仅构建指令期间可用；SSH mount 用于拉私有 Git 仓库；GIT_AUTH_TOKEN/GIT_AUTH_HEADER 是远程 context 与 ADD 拉私有仓库的预定义密钥。

> **关键点**：`docker build --secret id=aws,src=$HOME/.aws/credentials` + `RUN --mount=type=secret,id=aws`；可用 `env=` 挂环境变量、`target=` 自定义路径；**勿用 ARG/ENV 传密钥**——provenance mode=max 会暴露 build arg 值。

### 6. SBOM 成分清单（SBOM attestation）

`--sbom=true` 或 `--attest type=sbom` 生成 SPDX 格式软件物料清单（默认 BuildKit Syft scanner，基于 Anchore Syft），记录镜像内全部包与版本；local exporter 输出 `sbom.spdx.json`。

> **关键点**：默认只扫最终阶段；`ARG BUILDKIT_SBOM_SCAN_STAGE=true` 扩展扫描构建阶段（支持逗号列表），`BUILDKIT_SBOM_SCAN_CONTEXT=true` 扫上下文；这两个 ARG 不能变量替换、只能用显式 ARG 设置；`docker buildx imagetools inspect --format "{{ json .SBOM.SPDX }}"` 查看。

### 7. Provenance 构建溯源（Provenance attestation）

`--provenance=true` 生成 SLSA 格式构建溯源（mode=min 默认 / max），记录构建时间戳、frontend、材料、源码仓库与 revision、构建平台；mode=max 额外含 LLB 定义与完整 Dockerfile（base64）。

> **关键点**：mode=min 不泄露 build arg/secret 身份，所有构建安全可用；mode=max 分析价值更高但暴露 build arg 值（须配合 secret mount 重构）；版本支持 v0.2（默认）/v1；`--provenance` 默认开启（min）。

### 8. Attestation 生成与挂载流程

![[assets/docker/diagram-docker-attestation-flow.svg]]

*图：SBOM 与 Provenance 由 BuildKit 生成后封装为 in-toto Statement，作为 manifest 挂到 image index；classic image store 不支持，需 containerd image store 或 docker-container driver。*

### 9. Docker Scout 漏洞扫描与策略

Scout 分析镜像漏洞（`docker scout cves --only-package express`）并做供应链策略评估（`docker scout quickview`，6 项内置策略：无 copyleft 许可 / 默认非 root 用户 / 无可修复高危漏洞 / 无高知名度漏洞 / 基础镜像最新 / 供应链 attestation）。

> **关键点**：策略缺失数据（?）多为镜像缺 provenance+SBOM attestation——补 `--provenance=true --sbom=true` 重建；Default non-root user 策略违反时在 Dockerfile 加 `USER` 指令；评估前需 `docker scout enroll` + repo enable + config organization。

### 10. Build policies 构建策略（实验特性）

buildx 0.31.0+ / BuildKit 0.27.0+ 的供应链安全实验特性：用 OPA Rego 声明式策略校验构建输入（镜像须 digest 引用、须有 provenance/cosign 签名、Git 标签须 PGP 签名、远程构件须 HTTPS+checksum），策略文件 `Dockerfile.rego` 与 Dockerfile 同目录自动加载。

![[assets/docker/diagram-docker-build-policies-flow.svg]]

*图：Build policies 校验流程——构建时 Buildx 先解析全部输入 → 找同名 .rego 策略 → 构建前逐输入评估 → 全过才允许构建。*

> **关键点**：用例：强制基础镜像标准、校验第三方依赖、保证签名发布、合规审计、开发/生产差异化规则。

---

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方出处 |
|------|------|----------|----------|
| apt-get update 与 install 分开两个 RUN，构建出陈旧甚至失败的软件包 | update 层被缓存复用；修改 install 指令时 Docker 视 update 未变仍用缓存，索引过期 | 始终合并为 `RUN apt-get update && apt-get install -y --no-install-recommends <pkg>`（cache busting）；或版本 pin 如 `s3cmd=1.1.*` 强制缓存失效；层尾加 `&& rm -rf /var/lib/apt/lists/*` | best-practices RUN/apt-get 节 |
| ENV 设置变量后用 unset 无效，运行时变量仍在 | 每条 ENV 指令创建独立中间层，镜像层不可变，unset 只在后续层生效 | 用单条 RUN 完成设置-使用-清除：`RUN export ADMIN_USER=mark && echo $ADMIN_USER > ./mark && unset ADMIN_USER`；或把逻辑放进 shell 脚本由 RUN 执行 | best-practices ENV 节 |
| `RUN wget -O - url \| wc -l` 中 wget 失败但构建仍成功 | Docker 用 /bin/sh -c 执行，只评估管道最后一个命令（wc）的退出码 | 加 `set -o pipefail &&` 前缀；Debian 默认 dash 不支持 pipefail，用 exec 形式 `RUN ["/bin/bash", "-c", "set -o pipefail && wget ..."]` 显式选 bash | best-practices RUN/Using pipes 节 |
| 用 ARG/ENV 传 API token，开启 provenance mode=max 后凭据泄露 | mode=max 的 provenance 记录 build arg 值；ARG/ENV 值本身也落层 | 改用 secret mount：`docker build --secret id=token,env=API_TOKEN` + `RUN --mount=type=secret,id=token`；secret 不落层、不入缓存、永不进 provenance | SLSA provenance Mode/Max 警告 + Build secrets |
| docker driver 构建带 attestation 报 `ERROR: Attestation is not supported for the docker driver` | attestation 以 manifest 挂 image index，classic image store 不支持 image index | 启用 containerd image store，或改用 docker-container/kubernetes/remote driver；不换则 `--provenance=false --sbom=false` 显式关闭；`--load` 回 daemon 同样受限，`--push` 到 registry 则保留 attestation | Build attestations — Driver and image store support |
| 基础镜像 tag 漂移导致不可复现构建（3 个月前 alpine:3.21 指向 3.21.1，现在指向 3.21.4） | image tag 可变，发布者可更新 tag 指向新镜像；同一 tag 每次构建不保证同一版本 | pin digest：`FROM alpine:3.21@sha256:a8560b...`；用 Scout Up-to-Date Base Images 策略检测 pin 过期（non-compliant），Scout remediation 在新 digest 可用时自动开 PR 更新 | best-practices Pin base image versions |
| 大构建上下文拖慢构建：COPY . . 后改任意源码都触发 npm install 全量重装 | 上下文含 node_modules/临时文件；COPY . . 一次拷入全部，任何文件变化使后续层全部失效 | 根目录建 .dockerignore（node_modules、tmp* 等）；拆分 COPY：先 COPY package.json + lock 文件 → RUN npm install → 再 COPY . . → RUN npm build；临时生成构件用 `--mount=type=bind` 替代 COPY | Build cache optimization — Order your layers / Keep the context small |

---

## 最佳实践

### 1. 选择可信且最小的基础镜像，构建/运行双镜像

认准 Docker Official Images / Verified Publisher / DSOS 徽章，选最小满足需求的基础镜像（官方推荐 Alpine <6MB）；构建与单元测试用完整镜像，生产用更瘦镜像（无需编译器/调试工具）；减少依赖即减少漏洞面。
*官方出处：Dockerfile best practices — Choose the right base image*

### 2. 定期重建镜像，--pull 与 --no-cache 用途分明

镜像不可变，重建才是更新快照的唯一方式；`--pull` 强制检查并拉取新基础镜像（即使本地有缓存），`--no-cache` 禁用构建缓存强制全量重跑（拉最新依赖），两者可组合 `docker build --pull --no-cache`；配合 pin digest + Scout 自动更新。
*官方出处：Dockerfile best practices — Rebuild your images often*

### 3. 容器保持 ephemeral（无状态）

Dockerfile 定义的镜像应能最小配置即停止-销毁-重建-替换，参照 12-factor 的 Processes 原则；避免把状态写进容器层。
*官方出处：Dockerfile best practices — Create ephemeral containers*

### 4. 服务无需特权时用非 root 用户运行

`RUN groupadd -r app && useradd --no-log-init -r -g app app` 后 `USER app`；关键场景显式 UID/GID（默认分配不确定）；useradd 需 `--no-log-init` 规避 Go tar 稀疏文件 bug 导致的 /var/log/faillog 磁盘耗尽；避免 sudo（TTY/信号转发不可预测），需要提权用 gosu；不要频繁切换 USER。
*官方出处：Dockerfile best practices — USER 指令节*

### 5. 构建期密钥一律走 secret/SSH mount，不用 ARG/ENV

`docker build --secret id=aws,src=...` 传密钥，Dockerfile 用 `RUN --mount=type=secret,id=aws` 消费（默认挂 /run/secrets/<id>，可用 target/env 定制）；拉私有 Git 用 `--ssh` 或 GIT_AUTH_TOKEN/GIT_AUTH_HEADER（按 host 后缀区分多主机）；secret 永不落层、不进缓存、不进 provenance。
*官方出处：Build secrets*

### 6. 默认开启 provenance，生产镜像附加 SBOM

BuildKit 默认给镜像加 provenance mode=min（安全不泄密，记录时间戳/材料/源码 revision）；生产加 `--sbom=true` 记录完整成分清单；两者以 in-toto 格式挂 image index，任何人无需拉全镜像即可 inspect；构建前用 local exporter 验证 sbom.spdx.json。
*官方出处：Build attestations*

### 7. 多行参数按字母序排序并加注释

apt-get 安装列表每行一个包、字母序排列（参考 buildpack-deps），避免重复包、diff 易读、维护省心；反斜杠前加空格提升可读性；复杂 RUN 用 heredoc（`RUN <<EOF`）替代 && 链。
*官方出处：Dockerfile best practices — Sort multi-line arguments*

---

## 排查命令

```bash
# 构建期密钥注入（不落层）
docker build --secret id=aws,src=$HOME/.aws/credentials -t myapp .
# Dockerfile 内消费密钥（默认挂 /run/secrets/<id>）
RUN --mount=type=secret,id=aws cat /run/secrets/aws

# 构建带 attestation 的镜像（provenance 默认开启，SBOM 显式加）
docker build --sbom=true --provenance=true -t myapp .
# 本地导出验证 SBOM 文件
docker build --sbom=true --provenance=true --output type=local,dest=out .

# 查看镜像的 SBOM / Provenance attestation
docker buildx imagetools inspect --format "{{ json .SBOM.SPDX }}" myapp:latest
docker buildx imagetools inspect --format "{{ json .Provenance.SLSA }}" myapp:latest

# Docker Scout 漏洞分析与策略评估
docker scout cves --only-package express myapp:latest
docker scout quickview myapp:latest

# 重建强制拉新基础镜像 + 禁用缓存
docker build --pull --no-cache -t myapp .

# 检查当前 driver 是否支持 attestation（docker driver 不支持）
docker info --format '{{.Driver}}'
# 切换 containerd image store 或使用 docker-container driver
docker buildx create --use --driver=docker-container

# Build policies（实验特性）：Dockerfile 同目录放 Dockerfile.rego 后正常构建
# 构建前自动加载并逐输入评估，不合规即失败
docker build -t myapp .
```

---

## 相关笔记

- [[dockerfile-best-practices]] — Dockerfile 指令规范、多阶段构建、镜像优化入门（基础篇，2026-07-29）
- [[docker-buildkit-cache-supplychain-security]] — BuildKit 缓存机制、多阶段高级模式、供应链安全全景（深度篇，2026-08-11）
- [[docker-compose-swarm-security-deep-dive]] — Compose / Swarm 编排安全
- [[docker-advanced-compose-swarm-security]] — Docker 进阶：Compose / Swarm / 安全

本篇侧重「镜像优化与安全执行链路」：构建密钥注入、SBOM/Provenance attestation、Scout 扫描与策略、Build policies 构建前校验，与前两篇构成 Dockerfile 主题三件套。
