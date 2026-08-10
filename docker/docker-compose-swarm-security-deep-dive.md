---
created: 2026-08-10
source: Docker Official Docs
topic: Docker 进阶深度篇 — Compose 配置治理 / Swarm 运维纵深 / 供应链与运行时安全
priority: 🔴最高
theme: docker
---

# Docker 进阶深度篇 — 配置治理 · Swarm 运维 · 供应链与运行时安全

## 概述

本篇为 Docker 进阶主题的**深度篇**，从「概念了解」走向「生产可用」，全部取材 Docker 官方文档细节层，与 2026-07-27 基础篇（[[docker-advanced-compose-swarm-security]]）互补：

- **① Compose 配置治理** — 环境变量五级优先级、变量插值语法（`${VAR:-default}` / `${VAR:?err}`）、profiles 按需启用、多文件合并规则（单值替换/多值拼接）、watch 开发热更新（sync/rebuild/sync+restart）、init 容器 pre_start 与 post_start/pre_stop 生命周期钩子
- **② Swarm 运维纵深** — 节点 PKI 与 mTLS（证书 3 个月轮换、CA 交叉签名轮换）、节点可用性管理（Active/Pause/Drain）、14 态任务状态机、configs 与 secrets 分工（config 不可变需改名轮换）、overlay 控制面恒加密/数据面 `--opt encrypted`、manager autolock 密钥保护
- **③ 镜像供应链与运行时安全** — Docker Content Trust（⚠️ 2026-08 关键动态：Notary v1 于 **2026-12-08 退役**）与 dockerd trustpinning 新方案、Scout 本地策略评估与 CI 集成（`--exit-code` / policy-config）、BuildKit 构建密钥（`--mount=type=secret`）、userns-remap、daemon socket 保护（SSH context / TLS 2376）

## 架构图

### 三层纵深架构总览

配置治理产出服务定义 → Swarm 调度执行 → 安全机制在构建与运行两个环节分别把关。

![[assets/docker/diagram-docker-deep-arch.svg]]

## 核心概念

### Compose 配置治理

#### 1. 环境变量五级优先级

同一环境变量在多个来源定义时，Compose 按固定优先级解析：

1. `docker compose run -e`（最高）
2. `environment` / `env_file` 属性中经 shell 或 `.env` **插值后的值**
3. 仅 `environment` 属性
4. 仅 `env_file` 属性
5. 镜像 Dockerfile 的 `ENV` 指令（最低）

> **关键点**：`.env` 文件只参与插值，**本身不直接注入容器**。用 `docker compose config` 可查看最终解析结果。

#### 2. 变量插值语法

Compose 文件支持 `${VAR}` 与 `$VAR` 两种写法；带花括号时支持：

- `${VAR:-default}` — 未设置时用默认值
- `${VAR:?error}` — 未设置即报错退出（必填校验）
- `${VAR:+replacement}` — 已设置时用替代值

变量未定义且无默认值时**插值为空字符串**。

> **关键点**：生产环境给关键变量加 `${VAR:?error}` 强制校验，防止空值导致非法配置（如镜像引用变成 `postgres:`）。

#### 3. profiles 按需启用

服务可通过 `profiles` 属性归组，未启用对应 profile 时服务不启动；**无 profiles 属性的核心服务始终启用**。启用方式：

- `docker compose --profile debug up`
- 环境变量 `COMPOSE_PROFILES=debug`
- `--profile "*"` 启用全部
- 命令行显式指定某服务时其 profile 自动激活

> **关键点**：核心服务不设 profiles；调试/一次性工具（phpmyadmin、db-migrations）归入 profile 按需启动。

#### 4. 多文件合并规则

Compose 默认合并 `compose.yaml` 与 `compose.override.yaml`，也可用 `-f` 指定多个文件按顺序合并：

- **单值选项**（image、command、mem_limit）— 后者替换前者
- **多值选项**（ports、expose、external_links、dns、dns_search、tmpfs）— **拼接**而非替换
- 所有路径相对第一个（base）文件解析

> **关键点**：列表类选项拼接、标量类选项覆盖；合并后务必 `docker compose config` 审查最终配置。

#### 5. watch 开发热更新

`develop.watch` 定义文件监听规则，配合 `docker compose up --watch` 使用，仅对带 `build` 属性的本地源码服务生效。三种动作：

| 动作 | 行为 | 适用场景 |
|------|------|---------|
| `sync` | 文件同步进容器 | 热重载框架 |
| `rebuild` | BuildKit 重建镜像并替换容器 | 编译型语言或依赖变更 |
| `sync+restart` | 同步后重启容器主进程 | 配置文件变更 |

支持 `ignore` 与 `initial_sync`，规则自动套用 `.dockerignore`。

> **关键点**：watch 不是 bind mount 替代品而是补充——可忽略 node_modules 等目录提升性能与跨平台性。

#### 6. init 容器与生命周期钩子

`pre_start` 是 Compose 新增的 init 容器机制：在服务容器**创建后、启动前**，以独立临时容器顺序执行 setup 步骤（数据库迁移、卷权限修复），任一步非 0 退出则服务不启动；步骤成功后后续 `up` 默认跳过。`post_start` / `pre_stop` 钩子在服务容器内执行，可用更高权限（如 root）完成注册、备份等操作。

> **关键点**：pre_start 替代 one-shot service + `service_completed_successfully` 模式；挂载共享卷、继承服务镜像与网络。

### Swarm 运维纵深

#### 7. 节点 PKI 与 mTLS

Swarm 内置 PKI：`docker swarm init` 生成根 CA 与密钥，节点加入时经 join token（含 CA 证书 digest + 随机密钥）认证，manager 为每个节点签发证书（CN=随机节点 ID，OU=节点角色），节点间以**最低 TLS 1.2** 双向认证并加密通信。节点证书默认 **3 个月**轮换，可用 `docker swarm update --cert-expiry` 调整（最小 1 小时）。

> **关键点**：CA 泄露时执行 `docker swarm ca --rotate`：先生成交叉签名中间证书过渡，全部节点换新证后旧 CA 被遗忘，join token 同时失效。

#### 8. 节点可用性管理

`docker node ls` 的 AVAILABILITY 列：

| 状态 | 行为 |
|------|------|
| Active | 可调度 |
| Pause | 不派新任务但存量任务继续跑 |
| Drain | 不派新任务且存量任务被迁移到其他节点（用于维护） |

MANAGER STATUS 列：Leader / Reachable / Unavailable。节点标签（`docker node update --label-add`）可配合服务约束精确定位调度（如 PCI-SS 合规机器）。

> **关键点**：维护节点先 drain；manager 节点建议 drain 避免跑工作负载；promote/demote 调整角色但必须保持 Raft 多数派。

#### 9. 任务状态机

任务是原子调度单元，状态**单向推进不回退**：

![[assets/docker/diagram-swarm-task-state.svg]]

*图：Swarm 任务状态机——NEW → PENDING → ASSIGNED → ACCEPTED → READY → PREPARING → STARTING → RUNNING → COMPLETE/FAILED，另有 SHUTDOWN、REJECTED、ORPHANED、REMOVE*

- REJECTED — worker 拒绝任务
- ORPHANED — 节点宕机过久
- REMOVE — 服务删除/缩容
- `docker service ps` 查看 CURRENT STATE 与持续时间

> **关键点**：任务状态不会倒退（COMPLETE 不会回 RUNNING）；COMPLETE/FAILED 是终态。

#### 10. configs 与 secrets 分工

| 维度 | configs | secrets |
|------|---------|---------|
| 用途 | 非敏感配置（nginx.conf） | 敏感凭据 |
| 存储 | 不加密存储 | 加密存储 |
| 挂载 | 直接挂载文件系统，默认 `/<config-name>`，权限 0444（可设 uid/gid/mode） | 内存文件系统（RAM）挂载到 `/run/secrets/` |
| 上限 | 单文件 500KB | — |
| 可变性 | **不可变**，更新需以新名字创建后 `--config-rm/--config-add` 轮换 | 更新即新建 |

configs 支持 golang 模板引擎（`--template-driver golang`）。

> **关键点**：配置带版本号命名（site-v2.conf 模式）便于轮换回滚；运行中服务占用的 config 无法直接删除。

#### 11. 网络与流量加密

Swarm 产生两类流量：

- **控制面**（管理消息、join/leave）— **恒加密**
- **应用数据面**（容器间及外部流量）— 默认**不**加密

overlay 网络管理跨节点通信，ingress 是特殊的 overlay（IPVS 负载均衡），docker_gwbridge 桥接 overlay 与宿主机物理网络。节点间需开放 **7946 TCP/UDP**（发现）与 **4789 UDP**（overlay 数据面）。

> **关键点**：数据面加密用 `docker network create --driver overlay --opt encrypted` 启用 vxlan 层 IPSEC，有明显性能开销，生产前必须压测；ingress 默认不加密。

#### 12. manager autolock

Raft 日志默认加密落盘，autolock 进一步将 mTLS 密钥与 Raft 日志密钥**交还用户保管**：`docker swarm init --autolock` 或 `docker swarm update --autolock=true` 开启，生成 `SWMKEY-1-...` 解锁密钥；daemon 重启后必须 `docker swarm unlock` 才能使用集群，否则报 `Swarm is encrypted and needs to be unlocked`。

> **关键点**：解锁密钥存入密码管理器并定期 `docker swarm unlock-key --rotate`；新节点加入无需解锁（密钥经 mTLS 传播）。

### 供应链与运行时安全

#### 13. Docker Content Trust 与 dockerd trustpinning

DCT 基于 Notary v1 服务对镜像 tag 做数字签名（根密钥离线保管、仓库/委托密钥签名、timestamp 密钥保证新鲜度），客户端设 `DOCKER_CONTENT_TRUST=1` 后仅允许拉取/运行已签名镜像。

> ⚠️ **关键动态**：Notary v1 服务 notary.docker.io 将于 **2026-12-08 关闭**（DCT 正式退役）。新方案：**dockerd 内置 trustpinning**——在 daemon.json 配置指定根密钥，daemon 层强制仅接受该密钥签名的镜像。

#### 14. BuildKit 构建密钥与 SSH 挂载

构建期敏感信息（API token、SSH key、私仓凭据）**不应经 ARG/ENV 传递**——会固化进镜像层。正确做法：

```bash
docker build --secret id=aws,src=~/.aws/credentials ...
```

```dockerfile
# Dockerfile 中仅该条 RUN 指令期间可见
RUN --mount=type=secret,id=aws \
    aws s3 cp s3://bucket/data ./data
# 拉取私有 Git 仓库
RUN --mount=type=ssh git clone git@github.com:org/private.git
```

> **关键点**：secret 挂载只在构建指令执行期间存在，不会进入镜像层；可设 target/env 自定义挂载方式。

#### 15. Scout 策略本地评估与 CI 门禁

`docker scout policy` 在本地完成 SBOM 索引、CVE/VEX 富化与策略评估，**不上传数据**、多数场景无需组织账号。内置 7 条策略：

- 可修复的 CRITICAL/HIGH 漏洞
- 高危知名度漏洞（Log4Shell 等，含 CISA KEV）
- copyleft 许可证
- 过时基础镜像
- 供应链证明（provenance/SBOM attestation）
- 默认非 root 用户
- 未批准基础镜像

可用 `--policy-config` JSON 调阈值（如 grace_period_days）。

> **关键点**：CI 中 `docker scout policy <image> --exit-code` 或 `docker/scout-action@v1`（command: policy）作为镜像质量门禁。

#### 16. userns-remap 用户命名空间重映射

daemon 配置 `userns-remap` 后，容器内 root（UID 0）被映射为宿主上无特权的从属 UID（由 `/etc/subuid`、`/etc/subgid` 分配，如 `testuser:231072:65536`，231072 在命名空间内表现为 0），逃逸容器也无法获得宿主 root 权限。可用 `default` 让 Docker 自动创建 dockremap 用户。

> **关键点**：启用后现有镜像/容器层被掩盖（数据挪到 /var/lib/docker 子目录），建议新装环境启用；内核仅使用前 5 个映射区间。

### 供应链与运行时安全流水线

![[assets/docker/diagram-supply-chain-pipeline.svg]]

*图：从构建期密钥挂载、attestation 签名、Scout 策略门禁，到运行时最小权限分层加固与 daemon 访问保护的全链路*

## 常见问题表

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| 同一环境变量在多个来源定义，容器内最终值不是预期值（如 .env 与 environment 属性冲突） | 未理解 Compose 五级优先级：run -e > 插值后的 environment/env_file > environment > env_file > 镜像 ENV；.env 只参与插值不注入容器 | 按优先级表逐级排查；`docker compose config` 查看最终解析结果；避免同一变量多处定义，敏感值统一走 secrets |
| Compose 启动报镜像引用非法（如 `postgres:` 无 tag）或配置静默缺失 | 变量未定义时插值为空字符串：`${POSTGRES_VERSION}` 未设置且无默认值，解析成 `postgres:` | 关键变量用 `${VAR:?error}` 必填校验或 `${VAR:-default}` 提供默认值；.env 缺失时检查项目目录位置；CI 中先 `docker compose config` 验证 |
| 长期运行的 Swarm 集群出现节点间通信失败、新节点加入认证异常 | 节点 TLS 证书默认 3 个月过期未自动续期，或 CA 私钥泄露导致集群信任链失效 | 用 `docker swarm update --cert-expiry` 规划轮换期（最短 1 小时）；CA 泄露时 `docker swarm ca --rotate`，交叉签名中间证书过渡，全部节点换证后旧 CA 与旧 join token 一并失效 |
| Docker 重启后 `docker service ls` 报错：Swarm is encrypted and needs to be unlocked | 开启了 autolock，daemon 重启后 mTLS 密钥与 Raft 日志加密密钥需要人工解锁 | `docker swarm unlock` 输入 SWMKEY 解锁密钥；密钥丢失且集群有 quorum 时用 `docker swarm unlock-key` 找回；密钥存入密码管理器并定期 `docker swarm unlock-key --rotate` |
| 节点维护期间任务被调度到该节点，或存量服务容器被强制停机 | 未调整节点可用性，scheduler 仍认为节点 Active 可分配任务 | 维护前 `docker node update --availability drain <node>`（存量任务自动迁移），维护完改回 Active；仅暂停新任务用 Pause；manager 节点建议常驻 Drain |
| `docker config rm` 报 config is in use by the following service，且修改配置后服务不生效 | config 不可变（immutable），运行中服务占用的 config 无法删除，旧内容无法原地修改 | 以新名字创建新 config（如 site-v2.conf），`docker service update --config-rm site.conf --config-add source=site-v2.conf,target=...` 轮换，验证重部署后删除旧 config；命名带版本号便于回滚 |
| Swarm 集群跨节点应用流量明文传输，敏感业务数据可被网络抓包 | Swarm 数据面（应用流量）默认不加密，只有控制面恒加密；ingress 默认也不加密 | 创建 overlay 网络时加 `--opt encrypted` 启用 vxlan 层 IPSEC；有明显性能开销，生产前先压测；如需加密 ingress 流量需先自定义（删除重建）ingress 网络 |
| 构建镜像后发现 API token、SSH 私钥等凭据残留在镜像层中 | 用 ARG 或 ENV 向构建传递凭据，值被固化进镜像层，任何能拉取镜像的人都能提取 | 改用 `docker build --secret id=xxx,src=...` 传入，Dockerfile 内 `RUN --mount=type=secret,id=xxx` 临时挂载；拉私有 Git 仓库用 `--mount=type=ssh`；已泄露凭据立即轮换并重写镜像历史 |
| 启用 Docker Content Trust（DOCKER_CONTENT_TRUST=1）后拉取/推送签名镜像的服务即将不可用 | DCT 依赖的 Notary v1 服务 notary.docker.io 将于 **2026-12-08** 关闭，DCT 正式退役（2026-08 官方文档已标注） | 规划迁移：① dockerd 内置 trustpinning（daemon.json 配置根密钥，仅运行指定签名镜像）；② 结合 Scout 供应链证明（`--provenance=true --sbom=true` attestation）与策略门禁；③ 根密钥离线保管，迁移期间保留旧签名数据 |
| 开启 userns-remap 后，原有镜像和容器「消失」或无法访问 | 启用用户命名空间重映射会掩盖 /var/lib/docker 下既有镜像/容器层，资源被重新归属到子目录并调整属主 | userns-remap 应在全新 Docker 安装上启用而非存量环境；禁用后同样无法访问启用期间创建的资源；切换前备份或迁移数据 |
| 远程 Docker daemon 通过 TCP 端口暴露，被未授权客户端接管（提权风险） | daemon API 拥有 root 权限；以 HTTP 明文监听（如 `-H tcp://0.0.0.0:2375`）时任何能到达端口的人都能操控 daemon，且容器内也可访问 | 新版 dockerd 已拒绝无 TLS 的 TCP 监听；生产环境用 tlsverify + CA 证书双向认证（端口 2376，密钥 chmod 0400），或更简单：`docker context create --docker host=ssh://user@host` 走 SSH；仅从受信网络/VPN 可达 |
| 服务更新后任务反复重建但一直不健康 | 任务状态卡在 PREPARING/STARTING，或依赖的 config/secret 挂载目标冲突 | `docker service ps <svc>` 查看任务状态与错误；确认 config/secret 目标路径与容器内进程读取路径一致；检查资源约束（CPU/内存）是否满足 |

## 最佳实践

1. **部署前用 `docker compose config` 审查解析结果** — 所有变量插值、多文件合并、include/extends 解析完成后输出最终配置人工审查；配合 `config --environment` 检查插值变量来源，避免生产环境出现空值或意外覆盖。（[官方](https://docs.docker.com/manuals/compose/how-tos/multiple-compose-files/merge/)）

2. **敏感数据三级隔离：configs/secrets + BuildKit 密钥挂载** — 运行时非敏感配置用 swarm configs（可版本化轮换），敏感凭据用 secrets（加密 + RAM 挂载 /run/secrets/）；构建期凭据用 `--mount=type=secret`，绝不经过 ARG/ENV；Compose 单机场景用 top-level secrets 元素。（[官方](https://docs.docker.com/manuals/engine/swarm/configs/)）

3. **Swarm 证书生命周期管理：轮换演练 + 明确过期策略** — 按业务安全要求设置 `docker swarm update --cert-expiry`（默认 3 个月）；定期演练 `docker swarm ca --rotate` 全流程（交叉签名过渡、join token 更换、旧 CA 遗忘）；配合奇数 Manager + 固定 IP + autolock，形成管理面高可用与密钥保护闭环。（[官方](https://docs.docker.com/manuals/engine/swarm/how-swarm-mode-works/pki/)）

4. **autolock 密钥离线保管并定期轮换** — 开启 autolock 后，SWMKEY 解锁密钥立即存入密码管理器/离线保险库（丢失则 manager 重启后无法解锁）；定期 `docker swarm unlock-key --rotate` 并短暂保留旧密钥（防止轮换期间宕机的 manager 无法解锁）。（[官方](https://docs.docker.com/manuals/engine/swarm/swarm_manager_locking/)）

5. **Scout 策略作为 CI 镜像质量门禁** — CI 中执行 `docker scout policy <image> --exit-code`（或 docker/scout-action@v1 command: policy），用 `--policy-config` 调整阈值（如可修复漏洞 grace_period_days）；内置策略已覆盖可修复严重漏洞、Log4Shell 等高危 CVE、copyleft 许可证、非 root 用户、供应链证明、基础镜像白名单。（[官方](https://docs.docker.com/manuals/scout/policy/local/)）

6. **供应链可信：构建 attestation + 签名验证双保险** — 构建时加 `--provenance=true --sbom=true` 生成 SLSA provenance 与 SBOM attestation（Scout 据此做细粒度分析并满足供应链证明策略）；镜像签名方面规划从退役的 DCT 迁移到 daemon.json trustpinning 或第三方签名体系；基础镜像固定 digest 而非浮动 tag。（[官方](https://docs.docker.com/manuals/scout/explore/analysis/)）

7. **运行时最小权限分层加固** — 自上而下：镜像内 USER 非 root + Dockerfile 不用 root 启动；容器 `--cap-drop ALL` 仅加必需 capability（Docker 默认已用 allowlist 丢弃非必需项，但最佳实践是显式最小化）；daemon 级开启 userns-remap 或 Rootless；保持 seccomp 默认配置；daemon 远程访问走 SSH context 或 TLS 双向认证。（[官方](https://docs.docker.com/manuals/engine/security/)）

## 排查命令

```bash
# Compose 配置解析审查
docker compose config                          # 查看最终合并/插值结果
docker compose config --environment            # 检查插值变量来源
docker compose --profile debug up              # 按需启用 profile

# Compose watch 开发热更新
docker compose up --watch

# Swarm 节点与任务
docker node ls                                 # AVAILABILITY / MANAGER STATUS
docker node update --availability drain <node> # 维护前排空节点
docker service ps <svc>                        # 任务状态与持续时间
docker service update --cert-expiry 2160h0m0s <svc>  # 调整证书有效期

# Swarm 安全
docker swarm ca --rotate                       # CA 轮换（交叉签名过渡）
docker swarm unlock                            # autolock 解锁
docker swarm unlock-key --rotate               # 轮换解锁密钥
docker network create --driver overlay --opt encrypted <net>  # 数据面加密

# 供应链安全
docker build --secret id=aws,src=~/.aws/credentials ...        # 构建期密钥
docker scout policy <image> --exit-code        # 本地策略评估（CI 门禁）
docker scout policy <image> --policy-config policy.json         # 自定义阈值

# daemon 访问保护
docker context create --docker host=ssh://user@host <ctx>       # SSH context
```

## 相关笔记

- [[docker-advanced-compose-swarm-security]] — 基础篇（2026-07-27）：Compose 服务编排/depends_on/网络模型、Swarm 节点角色与服务模型、镜像安全与 Rootless 概览
- [[dockerfile-best-practices]] — Dockerfile 最佳实践与多阶段构建、层缓存
