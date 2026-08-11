---
created: 2026-08-11
tags: [docker, dockerfile, buildkit, cache, security, supply-chain]
source: Docker 官方文档
---

# Dockerfile 深度：BuildKit 缓存机制 / 多阶段高级 / 供应链安全

## 概述

本篇为 Dockerfile 主题**深度篇**，承接 [[dockerfile-best-practices]]（2026-07-29 基础篇：指令规范、多阶段基础、镜像优化入门），聚焦三大深度方向：

1. **BuildKit 构建缓存机制**：缓存失效判定规则、bind mount 与 cache mount 的区别、外部缓存后端（CI 场景）、GC 垃圾回收策略
2. **多阶段构建高级模式**：命名阶段、`--target` 定向构建、外部镜像作阶段、legacy builder 与 BuildKit 行为差异、可复用公共阶段
3. **镜像供应链安全**：digest 固定、构建密钥（secret/SSH mount）、SBOM/Provenance attestation、Docker Content Trust 退役与替代方案、运行时加固

全部素材取自 Docker 官方文档（`docker/docs` 仓库 `content/manuals/` 路径），包含缓存失效判定细节、GC 默认策略数值、attestation 存储格式等官方层面的具体信息。

---

## 架构图

### BuildKit 构建缓存架构

![[assets/docker/diagram-buildkit-cache-arch.svg]]

BuildKit 以层（layer）为单位管理缓存：指令字符串 + 文件校验和决定缓存是否命中；cache mount 提供跨构建累积的持久缓存；GC 策略按「48h 临时 → 60 天未用 → 容量上限」分层回收；多阶段构建最终只导出运行所需的最小产物，并附带 SBOM/Provenance attestation。

### 镜像供应链安全链路

![[assets/docker/diagram-supplychain-security-flow.svg]]

供应链安全贯穿全生命周期：源头选可信基础镜像并固定 digest → 构建期用 secret/SSH mount 注入密钥（不落层）、多阶段分离构建/运行环境、生成 attestation → 仓库侧用 Scout 扫描 CVE、签名验证 → 运行时非 root + 精简 capabilities。

---

## 核心概念

### 1. BuildKit 缓存失效规则

构建器逐条比较 Dockerfile 指令与缓存层，判定是否复用：

- **ADD/COPY 及带 bind mount 的 RUN**：通过文件元数据计算缓存校验和，文件内容/大小/权限等元数据变化即失效
- **mtime 不计入校验和**：仅修改时间变化不会失效缓存
- **其余指令（如 `RUN apt-get update`）**：只看指令字符串本身，不看容器内文件变化——这就是「update 单独一条 RUN」拿不到新包的原因
- 缓存一旦失效，**其后所有指令全部重建**

> **关键点**：secrets 内容不参与缓存判定（换密钥不破坏缓存）；secrets 的 ID 和挂载路径参与校验和（改了会失效）。

### 2. Bind mounts vs COPY

- `RUN --mount=type=bind`：将构建上下文临时挂载进构建容器，**默认只读**，指令结束后不持久化到镜像或缓存——适合「仅用于生成产物」的大源码上下文
- `COPY`：文件写入镜像层并进入缓存，即使最终镜像用不到
- 构建产物**必须写到挂载点之外**（挂载默认只读）

> **关键点**：大上下文仅用于产出一两个文件时用 bind mount，避免 COPY 把不必要文件永久塞进缓存层。

### 3. Cache mounts（持久缓存挂载）

`RUN --mount=type=cache,target=...` 提供跨构建累积的持久缓存：层重建时只下载新增/变更的包，未变包从缓存复用。常见配置：

| 工具 | 挂载路径 |
|------|---------|
| npm | `/root/.npm` |
| Go | `/go/pkg/mod` + `/root/.cache/go-build` |
| apt | `/var/cache/apt` + `/var/lib/apt`（加 `sharing=locked`） |
| pip | `/root/.cache/pip` |

> **关键点**：apt 需要同时挂 `/var/cache/apt` 与 `/var/lib/apt` 两个路径并加 `sharing=locked` 防并发写坏。

### 4. 外部缓存后端（external cache）

CI 构建器通常是临时的，本地 BuildKit 缓存无法跨任务复用。用 `--cache-to` / `--cache-from` 导入导出外部缓存：

| 后端 | 说明 |
|------|------|
| `inline` | 嵌入镜像本身，仅 image exporter，随镜像推送 |
| `registry` | 独立缓存镜像，推送到专用 ref |
| `local` | 写入本地目录 |
| `gha` | GitHub Actions 缓存（beta） |

- `mode=min`（默认）：只缓存最终导出的层
- `mode=max`：缓存全部中间层，命中率更高
- ⚠️ 缓存位置不可重复写，多分支要分 ref（`ref=...:<branch>`）

### 5. 构建密钥 Secret/SSH mounts

- **secret mount**：`docker build --secret id=aws,src=...` + `RUN --mount=type=secret,id=aws`，默认挂到 `/run/secrets/<id>`，可 `target` 改路径、`env` 转环境变量
- **SSH mount**：`docker build --ssh default` + `RUN --mount=type=ssh`，拉私有 Git 仓库用
- **Git 预定义密钥**：`GIT_AUTH_TOKEN`（x-access-token 基本认证，GitHub 风格）、`GIT_AUTH_HEADER`（自定义 Authorization，任意 Git 提供方）；按 host 后缀区分：`id=GIT_AUTH_TOKEN.github.com`

> **关键点**：**密钥勿用 ARG/ENV** —— 其值会持久化在镜像元数据、provenance attestation 与镜像历史中。

### 6. 多阶段构建高级模式

- **命名阶段**：`FROM ... AS build` + `COPY --from=build`，避免数字索引脆弱性（指令重排后不坏）
- **`--target` 定向构建**：`docker build --target build` 只构建到指定阶段——调试、测试（灌测试数据）、生产（真实数据）多态 Dockerfile
- **外部镜像作阶段**：`COPY --from=nginx:latest /etc/nginx/nginx.conf /nginx.conf`，可从任意 registry 镜像取文件
- **前一阶段续用**：`FROM builder AS build1/build2` 基于公共阶段派生多分支
- **legacy builder vs BuildKit 差异**：legacy 构建 `--target` 时会处理全部前置阶段；BuildKit **只构建目标阶段依赖链上的阶段**（不依赖的分支直接跳过）

### 7. 多平台构建

`docker buildx build --platform linux/amd64,linux/arm64` 构建多平台镜像（manifest list，注册表按宿主机架构自动选变体）。三种策略：

1. **QEMU 模拟**：`docker run --privileged --rm tonistiigi/binfmt --install all`，最简单但编译类任务明显变慢
2. **多原生节点**：`docker buildx create --append --name mybuild node-arm64`，性能最好但要维护节点集群
3. **交叉编译**（推荐）：`FROM --platform=$BUILDPLATFORM golang AS build` + `ARG TARGETOS` + `GOOS=$TARGETOS go build`，最快

预定义平台参数：`BUILDPLATFORM/BUILDOS/BUILDARCH/BUILDVARIANT` 与 `TARGETPLATFORM/TARGETOS/TARGETARCH/TARGETVARIANT`——全局可用，但**阶段内需 ARG 显式声明**。

### 8. GC 垃圾回收策略

BuildKit 按策略序列回收缓存（先具体后宽泛）：

1. 48h 未用的可再生成缓存（`type=source.local,type=exec.cachemount,type=source.git.checkout`）
2. 60 天未用的缓存
3. 超容量上限（`reservedSpace` / `maxUsedSpace` / `minFreeSpace`）

- **docker driver**：`daemon.json` 的 `builder.gc` 配置，`defaultKeepStorage` 默认 20GB（最简单调优入口）
- **其他 driver**：`buildkitd.toml` 配置
- ⚠️ 过滤器语法差异：daemon.json 用单等号 `type=source.local`，buildkitd.toml 用双等号 `type==source.local`

### 9. SBOM/Provenance Attestation

BuildKit 构建时默认附加 provenance（`mode=min`）attestation；`--sbom=true` 开启 SBOM，`--provenance=mode=max` 提升级别。格式为 **in-toto JSON**，作为独立 manifest 挂到 image index——**无需拉全镜像即可从 registry 检查镜像内容与来源**。

⚠️ 经典镜像存储（classic image store）不支持 image index，docker driver 构建带 attestation 会报错——需启用 containerd image store 或改用 `docker-container`/`kubernetes`/`remote` driver；不需要时 `--provenance=false --sbom=false` 关闭。

### 10. Docker Content Trust 退役（2026-12-08）

⚠️ DCT 依赖的 Notary v1 服务 `notary.docker.io` 将于 **2026-12-08 关闭**，官方已标注退役。替代方案：

- **daemon.json trustpinning**：签名验证内建 dockerd，只运行指定根密钥签名的镜像（管理员级强制，优于 CLI 级 DCT）
- **Scout attestation**：`--provenance=true --sbom=true` 供应链证明

DCT 密钥体系：离线 root key（**丢失不可恢复**，存硬件离线备份）+ repository/tagging key + server-managed timestamp key（新鲜度保证）。

---

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|---------|---------|
| `apt-get update` 单独一条 RUN，改 install 列表后拿到旧包 | RUN 缓存只看指令字符串，update 层命中缓存未重跑，源列表是旧的 | update 与 install 合并同一条 RUN；或版本固定 `package=1.3.*` 触发 cache bust | best-practices RUN/apt-get |
| RUN 管道命令失败但构建成功 | `/bin/sh -c` 只取管道最后命令的退出码 | `RUN set -o pipefail && wget ... \| wc -l`；dash 不支持 pipefail 时用 exec 形式 `RUN ["/bin/bash","-c","..."]` | best-practices Using pipes |
| ENV 设置后 unset 无效，值仍留在镜像 | 每条 ENV 建新中间层，unset 只影响后续层，旧值仍可被 inspect 读出 | 单条 RUN 内 shell 完成 set/use/unset；或 ARG+ENV 组合 | best-practices ENV |
| CI 每次全量重建、缓存不命中 | 指令顺序不当（COPY . . 在依赖安装前）；CI 临时构建器不共享本地缓存 | 先 COPY package*.json 再 RUN npm ci 最后 COPY 源码；`--cache-to/--cache-from type=registry,ref=...`，必要时 mode=max | cache/optimize |
| 文件没改但缓存失效 | 校验和基于文件元数据（内容/大小/权限），mtime 例外；SOURCE_DATE_EPOCH 设成 commit 时间戳会随每次提交破缓存 | 可复现构建用固定时间戳 `--build-arg SOURCE_DATE_EPOCH=0`；区分 mtime（不失效）与内容变化（失效） | cache/invalidation |
| 密钥经 ARG/ENV 传入，泄露进镜像历史 | ARG/ENV 值持久化在镜像元数据、provenance 与 history 中 | `--secret` + `RUN --mount=type=secret` 注入；SSH key 用 `--mount=type=ssh` | build/secrets |
| docker driver 构建带 attestation 报错 | 经典镜像存储不支持 image index | 启用 containerd image store 或改 docker-container driver；不需要就 `--provenance=false --sbom=false` | attestations |
| 构建缓存磁盘无限膨胀 | 缓存只按 GC 策略回收，长年构建可达数十 GB | daemon.json `builder.gc.defaultKeepStorage`（默认 20GB）；自定义策略按 reservedSpace/keepDuration/filter 分层；`docker builder prune` 手动清 | cache/garbage-collection |
| 拉私有 Git 仓库构建失败 `could not read Username` | 远程 Git 上下文/ADD 拉私有仓库时无认证 | `GIT_AUTH_TOKEN=$(gh auth token) docker build --secret id=GIT_AUTH_TOKEN ...`；GitLab 用 `GIT_AUTH_HEADER`；多 host 用后缀区分 | build/secrets Git auth |
| 基础镜像 tag 漂移，构建结果不可复现 | tag 可变，发布者可随时指向不同 digest | `FROM alpine:3.21@sha256:...` 固定 digest；定期 `--pull --no-cache` 重建；Scout Up-to-Date 策略检测过期 pin 并自动提 PR | best-practices Pin base image |

---

## 最佳实践

### 1. 选择可信且小的基础镜像
优先 Docker Official Images / Verified Publisher（带徽章）。开发/测试用胖镜像（含编译调试工具），生产用精简镜像；构建阶段与运行阶段分离——`FROM golang AS build` + `FROM alpine` 或 `scratch`。

### 2. 定期重建 + `--pull`/`--no-cache` 组合
镜像是不可变快照，含构建时点的基础镜像与依赖。`--pull` 强制取最新基础镜像，`--no-cache` 强制重跑所有步骤——两者用途不同，可组合 `docker build --pull --no-cache`。

### 3. 固定 digest 保证供应链完整性
`FROM alpine:3.21@sha256:...` 保证每次构建同一版本且有审计轨迹；配合 Scout Up-to-Date Base Images 策略，发布者更新时自动收到不合规信号并提 PR 升级——兼顾可复现与自动化安全修复。

### 4. 指令排序：昂贵稳定在前，易变在后
缓存失效导致其后所有层重建：`COPY package.json yarn.lock .` → `RUN npm install` → `COPY . .` → `RUN npm build`。多行参数按字母序排列（`apt-get install aaa bbb ccc`）便于维护与 review。

### 5. 用 `.dockerignore` 缩小构建上下文
排除 `node_modules`、`tmp*`、日志、构建产物——加速上下文传输、减小缓存。规则作用于整个上下文含子目录。

### 6. 产物用 bind mount，依赖缓存用 cache mount
仅用于生成产物的源码 `RUN --mount=type=bind,target=.`（不落缓存层）；包管理器缓存目录 `RUN --mount=type=cache,target=/root/.npm`（跨构建累积，重建只拉增量）。

### 7. CI 中导出外部缓存
`--cache-to type=registry,ref=user/app:buildcache,mode=max` + `--cache-from` 导入；按分支分 cache ref 避免互踩；mode=max 缓存全部中间层提高命中率。

### 8. 非 root 运行 + 明确 UID/GID
`RUN groupadd -r app && useradd --no-log-init -r -g app app` 后 `USER app`。显式 UID/GID 保证跨重建确定性；避免 sudo（TTY/信号转发不可预测），需要提权初始化时用 gosu；不要频繁切换 USER。

### 9. 构建密钥必须走 secret/SSH mount
ARG/ENV 值持久化在镜像元数据与 provenance 中，**不可用于机密**。`--secret`/`--ssh` 注入在构建时临时可用，不进入镜像层与缓存。

### 10. 开启 SBOM/Provenance attestation
构建时附加 in-toto attestation（默认 provenance mode=min，`--sbom=true` 加 SBOM），registry 中无需拉镜像即可检查镜像内容与来源；配合 Scout 扫描 + 策略评估形成完整供应链防线。

---

## 排查命令

```bash
# 查看构建缓存占用
docker system df

# 手动清理构建缓存
docker builder prune          # 清理未使用缓存
docker builder prune -af     # 全部清理（含使用中的）

# 强制重建（组合使用）
docker build --pull --no-cache -t my-image:my-tag .
docker build --no-cache-filter install .   # 只失效指定阶段

# 外部缓存导入导出（CI）
docker buildx build --push -t user/app:latest \
  --cache-to type=registry,ref=user/app:buildcache,mode=max \
  --cache-from type=registry,ref=user/app:buildcache .

# 多平台交叉编译
docker buildx build --platform linux/amd64,linux/arm64 -t multi .

# 漏洞扫描
docker scout cves --only-package express

# 查看 attestation（无需拉全镜像）
docker buildx imagetools inspect <image> --format '{{json .Manifest}}'

# 固定时间戳可复现构建
docker build --build-arg SOURCE_DATE_EPOCH=0 .
```

---

## 相关笔记

- [[dockerfile-best-practices]] — Dockerfile 基础篇：指令规范、多阶段构建入门、镜像优化基础（2026-07-29）。本文是其在缓存机制/供应链安全方向的深度延伸
- [[docker-advanced-compose-swarm-security]] — Docker 进阶：Compose 配置治理、Swarm 运维、供应链安全（Scout/AppArmor/Seccomp/Rootless 运行期加固）
- [[docker-compose-swarm-security-deep-dive]] — Compose/Swarm 安全深度篇
