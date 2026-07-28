---
created: 2026-07-27
source: Docker Official Docs
topic: Docker 进阶 — Compose / Swarm / 安全
priority: 🔴最高
theme: docker
---

# Docker 进阶 — Compose 多服务编排 · Swarm 集群模式 · 镜像安全与运行时安全

## 概述

Docker 进阶主题涵盖三大核心领域：**Compose** 用于多容器应用的定义与编排，**Swarm 模式**提供原生集群管理与服务编排，**镜像安全与运行时安全机制**确保容器化环境的安全基线。Compose 使开发者能够通过 YAML 描述整个应用栈（服务、网络、存储、密钥），Swarm 将多台 Docker 主机聚合成一个逻辑集群并提供声明式服务模型，Docker Scout、AppArmor、seccomp 和 Rootless 模式共同构建了从镜像供应链到容器运行时的纵深防御体系。

## 架构图

### 三层架构总览
![[assets/docker/diagram-docker-3layer-architecture.svg]]

### Swarm 集群架构
![[assets/docker/diagram-swarm-architecture.svg|303]]

### 安全纵深防御模型
![[assets/docker/diagram-security-layers.svg|697]]

## 核心概念

### 1. Compose 服务编排 — depends_on 与健康检查

Compose 通过 `depends_on` 控制服务启动顺序，支持三种条件：
- `service_started`（等待服务容器运行）
- `service_healthy`（等待健康检查通过）
- `service_completed_successfully`（等待一次性任务完成）

结合 `healthcheck` 指令定义数据库就绪检测，避免应用在依赖未就绪时启动失败。

> **关键点**：依赖启动顺序由 `depends_on.condition` 确定，但 Compose 只等待容器运行而非「已就绪」——必须显式配置 `healthcheck` 和 `service_healthy` 条件才能确保真正的就绪顺序。

### 2. Compose 网络模型 — 默认桥接与自定义拓扑

Compose 自动为项目创建默认桥接网络（`<project>_default`），容器通过服务名自动 DNS 解析互联。支持自定义网络拓扑隔离服务（如 proxy 和 db 分属不同网络），支持 `network_mode: host`（共享主机网络栈）、`network_mode: none`（无网络）、以及跨项目连接外部网络。

> **关键点**：服务名在内置 DNS 中注册，容器间始终通过服务名（而非 IP）通信——IP 在容器重建时动态变化，服务名稳定不变。

### 3. Compose 密钥管理 — 运行时敏感数据注入

Compose 支持 top-level `secrets` 元素定义密钥来源（本地文件/外部），通过 `services.secrets` 授予特定服务访问权限。密钥以文件形式挂载到容器内的 `/run/secrets/<name>` 路径，支持按服务粒度控制访问，避免通过环境变量泄露敏感数据。

> **关键点**：密钥优先于环境变量：环境变量可被所有进程读取、在日志中意外暴露；挂载为文件方式通过文件系统权限实现最小权限原则。

### 4. Swarm 节点与角色 — Manager/Worker 架构

Swarm 集群由 **Manager 节点**（管理集群状态、调度任务）和 **Worker 节点**（执行任务容器）组成。Manager 使用 **Raft 共识算法**保持集群状态一致，奇数个 Manager（推荐 3 或 5）保证容错。Manager 默认也作为 Worker，可配置为仅管理角色。

> **关键点**：Raft 要求多数派（quorum = N/2+1）共识：3 个 Manager 可容忍 1 个故障，5 个可容忍 2 个，2 个 Manager 容错为 0（无优势）。Manager 应使用固定 IP 地址防止重启后集群失联。

### 5. Swarm 服务模型 — 声明式与任务调度

Swarm 服务采用**声明式模型**：定义期望状态（镜像、副本数、端口、资源约束），Docker 自动维护此状态。支持 `replicated`（指定副本数分布到节点）和 `global`（每个节点运行一个任务）两种模式。任务（Task）是原子调度单元，包含容器和运行命令，一旦分配给节点不可迁移。

> **关键点**：修改服务配置后 Docker 自动执行滚动更新：停止旧任务、创建新任务，无需手动重启。服务 DNS 内置负载均衡，集群内通过服务名访问自动分发请求。

### 6. Swarm Ingress 路由网格

Swarm 的 Ingress 路由网格使集群中每个节点都能接受外部对已发布端口的请求，无论该节点是否实际运行该服务的容器。路由网格将请求转发至可用容器实例，支持通过 `--publish published=<port>,target=<container-port>` 发布端口，默认范围为 30000-32767。

> **关键点**：路由网格需要开放节点间 7946（TCP/UDP，容器网络发现）和 4789（UDP，Ingress 网络）端口；可通过 `--publish mode=host` 绕过路由网格让端口仅绑定到运行该服务的节点。

### 7. Docker Scout — 镜像漏洞分析与策略合规

Docker Scout 分析镜像内容生成 **SBOM**（软件物料清单），检测已知 CVE 漏洞并提供修复建议。支持 `docker scout cves` 命令查看漏洞详情，`docker scout policy` 命令定义供应链规则（严重性漏洞、合规许可证、基础镜像更新、非 Root 用户、许可基础镜像等）。策略评估在本地进行，不上传数据。

> **关键点**：Scout 内置策略类型包括：严重性漏洞（默认拦截可修复的 CRITICAL/HIGH）、合规许可证（GPL/AGPL 等）、过时基础镜像、高知名度漏洞（如 Log4Shell）、供应链证明、非 Root 用户检查、许可基础镜像白名单。

### 8. AppArmor — 强制访问控制（MAC）

AppArmor 是 Linux 内核安全模块，Docker 自动为容器生成并加载名为 `docker-default` 的默认 AppArmor 配置文件。该配置在提供广泛应用兼容性的同时施加适度保护。可通过 `--security-opt apparmor=<profile_name>` 加载自定义配置文件实现更严格的限制。

> **关键点**：Docker 默认的 AppArmor 配置生成于 tmpfs 并加载到内核，仅适用于容器而非 Docker 守护进程本身；自定义配置通过 `apparmor_parser` 加载后以 `--security-opt` 引用。

### 9. Seccomp — 系统调用过滤

Seccomp（Secure Computing Mode）限制容器可使用的 Linux 系统调用。Docker 默认 seccomp 配置允许约 260 个系统调用，禁用约 44 个（包括 `bpf`、`mount`、`kexec_load`、`io_uring` 等危险调用）。采用**允许列表模式**：默认拒绝所有调用（SCMP_ACT_ERRNO），仅明确允许的放行。

> **关键点**：Seccomp 是实现容器最小权限的关键机制——不建议修改默认配置。被拦截的显著调用包括：`bpf`（加载持久化 BPF 程序）、`io_uring`（容器逃逸漏洞）、`mount/umount`（修改文件系统）、`clone`（创建新命名空间）、`keyctl`（未命名空间化的内核密钥环）。

### 10. Rootless 模式 — 无 Root 运行 Docker 守护进程

Rootless 模式允许以非 root 用户身份运行 Docker 守护进程和容器，通过**用户命名空间**（user namespace）实现。与 `userns-remap` 模式不同（后者 daemon 仍有 root 权限），Rootless 模式下 daemon 和容器均无 root 权限，无需 SETUID 二进制文件或文件能力（除 `newuidmap/newgidmap` 外）。

> **关键点**：前提条件：安装 `uidmap` 包、`/etc/subuid` 和 `/etc/subgid` 为用户分配至少 65536 个从属 UID/GID。安装后需 `systemctl --user start docker.service` 并启用 `loginctl enable-linger` 以支持系统启动时自动运行。

## 常见问题与解决方案

| 问题 | 原因 | 方案 | 官方链接 |
|------|------|------|---------|
| Compose 依赖服务尚未就绪导致应用启动失败 | `depends_on` 仅确保依赖容器已运行（而非已就绪），数据库等服务在容器启动后仍需时间完成初始化 | 在 `depends_on` 中设置 `condition: service_healthy`，并为依赖服务配置 `healthcheck`。例如 postgres 使用 `pg_isready` 命令检测就绪状态（`interval: 10s, retries: 5, start_period: 30s`） | [Docker Docs](https://docs.docker.com/manuals/compose/how-tos/startup-order/) |
| Swarm 更新或节点重启后 Manager 集群失联（IP 变更） | Manager 节点使用动态 IP 地址，重启后 IP 变化，导致其他节点无法通过旧 IP 联系现有 Manager | 在 `docker swarm init` 时始终指定 `--advertise-addr` 为固定 IP 地址。Manager 节点必须使用静态 IP，Worker 节点可使用动态 IP。恢复时需从备份恢复 Raft 日志 | [Docker Docs](https://docs.docker.com/manuals/engine/swarm/admin_guide/#configure-the-manager-to-advertise-on-a-static-ip-address) |
| Swarm 失去 Manager 多数派（quorum lost），无法执行管理操作 | Raft 要求多数 Manager 节点在线才能达成共识。例如 3 个 Manager 中 2 个不可用时，集群无法调度新任务或处理成员变更 | 部署奇数个 Manager（3 或 5 个），使用 `--manager-node-liveness.period` 和磁盘监控确保 Manager 健康。恢复步骤：停止所有 Manager，用 `docker swarm init --force-new-cluster` 从剩余数据重建 | [Docker Docs](https://docs.docker.com/manuals/engine/swarm/admin_guide/#recover-from-losing-the-quorum) |
| 镜像中存在严重漏洞（Critical/High CVE） | 基础镜像或依赖包有已知漏洞（如旧版 express 4.17.1 的 CVE-2022-24999） | 使用 Docker Scout 扫描：`docker scout cves --only-package <name>` 定位漏洞包，升级到修复版本后重建镜像。配置 Scout Policy 自动拦截含可修复 CRITICAL/HIGH 漏洞的镜像 | [Docker Docs](https://docs.docker.com/manuals/scout/quickstart/) |
| 容器内进程以 root 运行带来安全风险 | Docker 默认容器内进程以 root 运行，虽然受命名空间限制，但一旦发生容器逃逸漏洞（如 io_uring 相关 CVE），攻击者获得宿主机 root 权限 | 组合使用多层防护：1）Dockerfile 中使用 `USER` 指令切换非 root 用户；2）启用 Rootless 模式（`dockerd-rootless-setuptool.sh install`）消除 daemon root 权限；3）seccomp 默认配置禁用危险系统调用（如 io_uring）；4）Scout Policy 的 Default Non-Root User 策略检查镜像是否设置非 root 用户 | [Docker Docs](https://docs.docker.com/manuals/engine/security/rootless/) |
| Compose 远程依赖（OCI registry 中的 include/extends）引入不可见的提权配置 | Compose 的 `include` 和 `extends` 支持从 OCI 镜像仓库引用 Compose 文件，嵌套依赖可能在用户未审查的情况下引入特权容器、主机卷挂载或未被信任的镜像 | 始终使用 `docker compose config` 审查完整解析后的配置（包括所有 resolved include/extends 和变量插值）。对远程来源保持警惕，理解每一层依赖请求的权限 | [Docker Docs](https://docs.docker.com/manuals/compose/trust-model.md) |
| 多 Compose 文件合并时配置被意外覆盖 | 使用 `-f` 合并多个 Compose 文件时，合并规则复杂（如列表替换而非追加），可能导致某些配置丢失或意外变更 | 使用 `include` 指令替代 `-f` 合并：`include` 采用明确的合并语义，支持条件包含和更清晰的依赖关系。对于复杂应用，使用 `extends` 继承特定服务配置而非全局文件合并 | [Docker Docs](https://docs.docker.com/manuals/compose/how-tos/multiple-compose-files/) |

## 官方最佳实践

### 1. Compose 生产就绪配置

生产环境 Compose 文件应：
1. 移除代码卷绑定（代码内置于镜像）
2. 绑定正确主机端口
3. 设置 production-specific 环境变量（降低日志冗度）
4. 指定 `restart: always` 策略避免停机
5. 添加日志聚合等额外服务

推荐定义 `compose.production.yaml` 通过 `-f` 覆盖基础配置。

官方参考：https://docs.docker.com/manuals/compose/how-tos/production/

### 2. Compose 密钥优先于环境变量

使用 `secrets` 元素而非环境变量传递敏感数据。密钥以文件挂载到 `/run/secrets/<name>`，通过标准文件系统权限控制访问，不会被日志意外记录。同时支持从文件、环境变量或外部密钥管理服务获取密钥内容。

官方参考：https://docs.docker.com/manuals/compose/how-tos/use-secrets/

### 3. Swarm Manager 高可用部署建议

1. 部署奇数个 Manager（推荐 3 个，大型集群 5 个）
2. Manager 使用固定 IP 地址
3. 将 Manager 分布在不同的故障域（物理机/可用区）
4. Manager 节点不运行工作负载（通过节点标签和约束排除）
5. 定期备份 Manager 节点的 Raft 日志目录（`/var/lib/docker/swarm`）

官方参考：https://docs.docker.com/manuals/engine/swarm/admin_guide/

### 4. Swarm 服务滚动更新与回滚

使用 `--update-parallelism`（同时更新的副本数）和 `--update-delay`（更新间隔）控制滚动更新节奏。失败时使用 `--rollback-monitor` 和 `--rollback-parallelism` 自动回滚。推荐为关键服务设置 `healthcheck`，Swarm 在健康检查失败时自动停止滚动更新。

官方参考：https://docs.docker.com/manuals/engine/swarm/services/

### 5. Docker Scout 策略集成到 CI 管道

在 CI 中集成 `docker scout policy` 命令，使用内置策略（Severity-Based Vulnerability、Default Non-Root User、Up-to-Date Base Images）作为镜像质量门禁。策略评估在本地进行，数据不外泄。可结合 Rego 自定义策略实现企业级合规检查。

官方参考：https://docs.docker.com/manuals/scout/policy/

### 6. 安全纵深防御：AppArmor + Seccomp + Rootless 三层防护

推荐按以下层次构建容器运行时安全：

1. **Rootless 模式**（防止 daemon 提权）
2. **Seccomp 默认配置**（防止危险系统调用）
3. **AppArmor 自定义配置**（限制文件系统/网络访问）
4. **Docker Scout 策略**（镜像供应链安全）
5. **Docker Content Trust**（镜像签名验证）

每一层提供独立的防护，单层失效不会导致整体沦陷。

官方参考：https://docs.docker.com/manuals/engine/security/

### 7. Compose 信任模型与依赖审查

始终运行 `docker compose config` 审查完整解析配置，检查 `include/extends` 链中每一层级的权限请求。熟悉 Compose 的 OCI 引用机制，不要自动接受远程来源的 include/extends。对于关键生产应用，考虑签名的 Compose 文件或自建内部 OCI registry 分发。

官方参考：https://docs.docker.com/manuals/compose/trust-model.md

## 排查命令速查

```bash
# Compose
docker compose config                    # 审查完整解析后的 Compose 配置
docker compose ps                        # 查看服务容器状态
docker compose logs -f <service>         # 跟踪服务日志
docker compose exec <service> <cmd>      # 在运行中的容器执行命令

# Swarm
docker node ls                           # 查看集群节点列表
docker node ps <node>                    # 查看节点上运行的任务
docker service ls                        # 查看所有服务
docker service ps <service>              # 查看服务任务分布
docker service logs <service>            # 查看服务日志
docker service scale <service>=<n>       # 扩缩容

# 安全
docker scout cves <image>                # 扫描镜像漏洞
docker scout policy <image>              # 策略合规评估
docker scout sbom <image>                # 查看 SBOM
docker inspect <container>               # 查看容器安全配置
docker info --format '{{.SecurityOptions}}'  # 查看 daemon 安全选项

# 集群恢复
docker swarm init --force-new-cluster    # 从剩余 Manager 数据重建集群
docker swarm join-token manager          # 获取 Manager 加入令牌
```

## 官方参考文档

| 文档 | 链接 |
|------|------|
| Docker Compose 入门指南 | https://docs.docker.com/manuals/compose/gettingstarted/ |
| Compose 网络 | https://docs.docker.com/manuals/compose/how-tos/networking/ |
| Compose 启动顺序控制 | https://docs.docker.com/manuals/compose/how-tos/startup-order/ |
| Compose 生产环境使用 | https://docs.docker.com/manuals/compose/how-tos/production/ |
| Compose 多文件使用 | https://docs.docker.com/manuals/compose/how-tos/multiple-compose-files/ |
| Compose 密钥管理 | https://docs.docker.com/manuals/compose/how-tos/use-secrets/ |
| Compose 信任模型 | https://docs.docker.com/manuals/compose/trust-model.md |
| Swarm 关键概念 | https://docs.docker.com/manuals/engine/swarm/key-concepts/ |
| Swarm 管理指南（Raft 与高可用） | https://docs.docker.com/manuals/engine/swarm/admin_guide/ |
| Swarm Raft 共识 | https://docs.docker.com/manuals/engine/swarm/raft/ |
| Swarm 路由网格 | https://docs.docker.com/manuals/engine/swarm/ingress/ |
| Swarm 服务部署 | https://docs.docker.com/manuals/engine/swarm/services/ |
| Swarm Stack 部署 | https://docs.docker.com/manuals/engine/swarm/stack-deploy/ |
| Swarm 密钥管理 | https://docs.docker.com/manuals/engine/swarm/secrets/ |
| Docker Scout 快速入门 | https://docs.docker.com/manuals/scout/quickstart/ |
| Docker Scout 策略评估 | https://docs.docker.com/manuals/scout/policy/ |
| AppArmor 配置文件 | https://docs.docker.com/manuals/engine/security/apparmor/ |
| Seccomp 配置文件 | https://docs.docker.com/manuals/engine/security/seccomp/ |
| Rootless 模式 | https://docs.docker.com/manuals/engine/security/rootless/ |

## 相关笔记
- [[docker-compose-basics]]
- [[docker-swarm-basics]]
- [[docker-security-basics]]
