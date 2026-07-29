---
created: 2026-07-29
tags: [docker, dockerfile, build, optimization, security]
source: Docker 官方文档
---

# Dockerfile 编写规范与镜像优化

## 概述

Dockerfile 是 Docker 构建镜像的命令脚本，通过一系列指令（FROM、RUN、COPY、CMD 等）定义镜像的构建过程。多阶段构建允许在一个 Dockerfile 中使用多个 FROM 语句，将构建环境和运行环境分离，极大缩小最终镜像尺寸。镜像优化包括选择合适基础镜像、利用构建缓存、减少层数、清理中间文件等技术。安全实践包括使用非 root 用户、定期更新基础镜像、扫描漏洞等。

Docker 镜像构建基于分层文件系统，每条指令创建一个新的可缓存层。BuildKit 是 Docker 的现代构建引擎，提供更好的并行性、缓存管理和安全性。

---

## 架构图

### 多阶段构建流程

![[assets/docker/diagram-multi-stage-build.svg]]

多阶段构建将 Dockerfile 分为多个阶段，每个阶段使用不同基础镜像。构建阶段编译应用，运行阶段仅包含最终产物，显著减小镜像尺寸并降低攻击面。

### Dockerfile 层顺序优化

![[assets/docker/diagram-layer-caching.svg]]

Docker 构建缓存基于层（layer）机制，合理排列指令顺序可最大化缓存命中率。将稳定不变的指令放在前面，频繁变化的源码放在最后。

---

## 核心概念

### 1. 多阶段构建 (Multi-stage Builds)

在一个 Dockerfile 中使用多个 FROM 语句，每个 FROM 开始一个新阶段。可以选择性地从前一阶段复制产物到最终阶段，丢弃构建工具和中间文件。

> **关键点**：使用 `FROM ... AS <name>` 命名阶段，通过 `COPY --from=<name>` 复制产物，避免依赖数字索引。

### 2. 构建缓存优化 (Build Cache Optimization)

Docker 检查每条指令是否可以从缓存中复用。构建缓存的核心策略：

- **合理排序指令**：将稳定的依赖安装放在前面，频繁变化的源码放在后面
- **使用缓存挂载**：`--mount=type=cache` 保持包管理器缓存
- **使用外部缓存**：`--cache-from` / `--cache-to` 在 CI/CD 中复用缓存

> **关键点**：先 COPY package.json + RUN install，再 COPY 源码；使用 `--mount=type=cache` 保持包管理器缓存。

### 3. 镜像优化 (Image Optimization)

| 策略 | 说明 | 效果 |
|------|------|------|
| 选择最小基础镜像 | Alpine 仅 5MB | 大幅减小镜像体积 |
| 减少包安装 | `--no-install-recommends` | 避免垂直依赖膨胀 |
| 清除 apt 缓存 | `rm -rf /var/lib/apt/lists/*` | 减少层体积 |
| 使用 .dockerignore | 排除不必要的构建文件 | 加速构建，减少上下文 |

### 4. COPY vs ADD 指令

- **COPY**：基本文件复制，推荐优先使用
- **ADD**：支持远程 URL 下载和 tar 自动解压
- 官方推荐优先使用 COPY，仅在需要远程资源下载或自动解压时使用 ADD
- ADD 支持 `--checksum` 验证远程文件完整性
- COPY 可配合 `--mount=type=bind` 替代临时文件复制

### 5. CMD 与 ENTRYPOINT 的区别

- **ENTRYPOINT**：设置镜像的主命令
- **CMD**：提供默认参数

组合使用时：`ENTRYPOINT ["/usr/bin/s3cmd"]` + `CMD ["--help"]`，则 `docker run s3cmd` 执行 `s3cmd --help`，而 `docker run s3cmd ls s3://bucket` 会覆盖 CMD 为 `ls s3://bucket`。

> **建议**：ENTRYPOINT 设置主命令 + CMD 设置默认参数，使用 exec 格式（JSON 数组）以接收 Unix 信号。

### 6. 非 root 用户安全实践

```dockerfile
RUN groupadd -r appgroup && useradd --no-log-init -r -g appgroup appuser
USER appuser
```

> **关键点**：添加 USER 指令是最简单有效的安全措施，可解决 Docker Scout 的 Default non-root user 策略违反。

---

## 常见问题与解决方案

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| apt-get update 缓存问题 | update 和 install 分开在两条 RUN 指令中 | 始终合并为 `apt-get update && apt-get install` 在同一条 RUN 中 |
| ENV 变量无法清除 | 每个 ENV 创建新层，之前层的变量仍在 | 在单条 RUN 指令中完成 export、使用、unset |
| 管道命令失败不报错 | Docker 只检查管道最后一个命令的退出码 | 在管道前添加 `set -o pipefail &&` |
| 多阶段构建引用错误 | 使用数字索引（FROM 0），指令重排序后索引改变 | 使用 `FROM ... AS build` 命名阶段，`COPY --from=build` |
| 缓存频繁失效 | 构建上下文包含不必要的文件 | 创建 .dockerignore，使用 `--mount=type=bind` |
| 基础镜像版本可变 | 标签可变（如 alpine:3.21 在不同时间点不同） | 使用 digest 固定版本 `FROM alpine:3.21@sha256:...` |
| BuildKit 与旧引擎行为差异 | 旧引擎构建所有阶段，BuildKit 只构建依赖链 | 启用 BuildKit：`docker buildx build` 或 `DOCKER_BUILDKIT=1` |

---

## 最佳实践

### 1. 使用多阶段构建分离构建和运行环境

构建阶段构建应用并生成产物，第二阶段仅复制产物到最小基础镜像（如 scratch 或 distroless）。最终镜像不包含编译器、构建工具和调试工具，显著降低攻击面。

### 2. 定期重建镜像保持最新安全补丁

Docker 镜像是不可变的快照。定期使用 `--pull` 获取最新基础镜像，配合 `--no-cache` 完整重建：

```bash
docker build --pull --no-cache -t my-image:tag .
```

### 3. 选择最小的合适基础镜像

- Alpine 仅 5MB，是官方推荐的最小基础镜像
- 考虑使用两种基础镜像：一个用于构建和单元测试，另一个（更精瘦）用于生产
- 始终从可信源选择镜像（Docker Official Images / Verified Publisher）

### 4. 利用 .dockerignore 减小构建上下文

创建 .dockerignore 文件排除不必要文件，类似 .gitignore：

```
node_modules/
.git/
*.md
*.log
```

### 5. 使用构建缓存挂载加速包管理器操作

```dockerfile
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y python3
```

各类包管理器示例：
- apt：`/var/cache/apt,sharing=locked`
- pip：`/root/.cache/pip`
- npm：`/root/.npm`
- go：`/go/pkg/mod`

### 6. 使用 Docker Scout 扫描和管理镜像漏洞

```bash
docker scout quickstart
docker scout policy --image my-image:tag
```

构建时添加资产证明和 SBOM：
```bash
docker build --provenance=true --sbom=true -t my-image:tag .
```

### 7. 使用外部缓存加速 CI/CD 构建

```yaml
# GitHub Actions 示例
- name: Build
  uses: docker/build-push-action@v6
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

---

## 常用排查命令

```bash
# 查看镜像分层
docker history my-image:tag

# 检查镜像大小
docker images my-image:tag

# 分析镜像内容
docker scout quickstart my-image:tag
docker scout policy my-image:tag

# 构建时调试（使用 BuildKit 输出）
DOCKER_BUILDKIT=1 docker build --progress=plain -t my-image:tag .

# 运行容器验证
docker run --rm -it my-image:tag /bin/sh

# 查看 BuildKit 构建缓存
docker builder prune --all
docker system df
```

---

## 相关笔记

- [[docker-compose-essentials]]
- [[multi-stage-build-examples]]
- [[container-security-basics]]

---

## 官方参考

- [Dockerfile Best Practices](https://docs.docker.com/build/building/best-practices/)
- [Multi-stage Builds](https://docs.docker.com/build/building/multi-stage/)
- [Optimize Cache Usage in Builds](https://docs.docker.com/build/cache/optimize/)
- [Docker Scout Quickstart](https://docs.docker.com/scout/quickstart/)
- [Docker Engine Security](https://docs.docker.com/engine/security/)
- [Dockerfile Reference](https://docs.docker.com/reference/dockerfile/)
- [Build Cache Invalidation](https://docs.docker.com/build/cache/invalidation/)
- [Docker Build Context and Dockerignore](https://docs.docker.com/build/concepts/context/#dockerignore-files)
- [BuildKit Overview](https://docs.docker.com/build/buildkit/)
