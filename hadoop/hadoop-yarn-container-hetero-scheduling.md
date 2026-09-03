---
created: 2026-09-03
topic: Hadoop 生态
subtopic: YARN 容器化运行时与异构资源调度深潜（Docker/runc 运行时 + GPU 异构资源 + Node Label 分区）
tags: [hadoop, yarn, docker, runc, gpu, nodelabel, linuxcontainerexecutor, 官方文档]
---

# YARN 容器化运行时与异构资源调度深潜

> 本篇为 Hadoop 主题第五轮轮转的新角度笔记，取「容器化执行环境 + 异构资源调度」方向：LinuxContainerExecutor 统一支持 default/docker/runc 三种容器执行形态的配置与安全模型、GPU 以可扩展资源类型进入 YARN 资源模型后的调度与设备隔离、Node Label 把集群切分为互斥/非互斥分区并配合队列 ACL 定向分配异构节点。与 [[hadoop-ecosystem]]（全景概述）、[[hadoop-core-internals]]（HDFS/YARN 核心机制）、[[hadoop-yarn-scheduling-ecosystem]]（YARN 调度容错与生态工具）、[[hadoop-mapreduce-execution-hdfs-storage]]（MR 执行机制与 HDFS 存储体系）互补。内容出自 Apache Hadoop 官方文档（DockerContainers / RuncContainers / UsingGpus / NodeLabel / CapacityScheduler）。

## 概述

YARN 自 Hadoop 3.x 起由 **LinuxContainerExecutor（LCE）** 统一支持三种容器执行形态：`default`（普通进程树）、`docker`（镜像级隔离）、`runc`（OCI 实验特性，镜像 squashFS 化后存 HDFS 分发）。应用通过 `YARN_CONTAINER_RUNTIME_TYPE` 等环境变量声明运行形态，MR/Spark 无需感知即可透传。GPU 以可扩展资源类型 `yarn.io/gpu` 进入资源模型，必须配 DominantResourceCalculator 做多维主导资源调度，再由 NM 资源插件 + cgroup devices 控制器实现设备级隔离。Node Label 把集群按互斥/非互斥分区切分，配合队列 ACL 与分区百分比容量，将 GPU/高内存等异构节点定向分配给指定队列。本轮要点：

- **LCE 与运行时框架**：container-executor SUID 二进制（root:hadoop 6050）+ container-executor.cfg（root:0400）白名单强制，`allowed-runtimes` 声明运行时集合
- **Docker 运行时**：yarn-site 策略层与 cfg 强制层双层配置、镜像要求与 RUN_OVERRIDE/ENTRYPOINT 两种启动模式、容器内用户管理三方案（静态/bind mount/SSSD）、特权容器两级放行模型
- **runC 运行时**：OCI 实验特性（YARN-9014），镜像经 docker_to_squash.py 转 squashFS 存 HDFS 分发，与 Docker daemon 解耦
- **GPU 异构调度**：resource-types.xml 声明 → DominantResourceCalculator 调度 → NM resource-plugins 发现 → cgroup devices 按 minor 号隔离
- **Node Label 分区**：exclusive/non-exclusive 分区语义、队列 accessible-node-labels 授权与分区容量、centralized/distributed/delegated-centralized 三种映射模式

## 架构图

![[assets/hadoop/diagram-yarn-container-runtime-arch.svg]]

*图：YARN 容器化与异构资源调度全景架构（LCE → 运行时层 → cgroup；GPU 与 Node Label 双路径）*

**架构要点**：

- **调度决策（RM 侧）**：DominantResourceCalculator 按多维主导资源（含 yarn.io/gpu）公平调度；Node Label 标签经 fs-store 持久化，CapacityScheduler 按队列 accessible-node-labels 与分区容量百分比把容器分配到标签分区
- **执行启动（NM 侧）**：LinuxContainerExecutor 以 SUID 二进制承载容器启动；`yarn.nodemanager.runtime.linux.type` 默认值与请求值 `YARN_CONTAINER_RUNTIME_TYPE` 共同决定落到哪个运行时；所有挂载/网络/能力/设备/镜像源最终过 container-executor.cfg 强制白名单
- **Docker 路径**：NM 调 daemon 执行 docker run 并传 `--cgroup-parent` 把容器纳入 YARN cgroup 树，镜像未缓存时 daemon 隐式 pull
- **runC 路径**：镜像先由 docker_to_squash.py 转为 squashFS layer + config + manifest 存入 HDFS `/runc-root`，NM 按 image-tag-to-hash 解析并本地化 layer 后用 runc 以 overlay/squashFS mount 启动，无 daemon 依赖
- **GPU 路径**：NM resource-plugins（auto 模式 nvidia-smi 自动发现）→ cgroup devices 控制器按 GPU minor 号隔离；Docker 场景经 nvidia-docker 插件（v1 端点 localhost:3476）注入 /dev/nvidia* 设备
- **资源隔离**：所有容器形态统一受 cgroup 限制 CPU/内存/设备，纳入同一棵 cgroup 树

## 核心概念

### LinuxContainerExecutor 与容器运行时框架

LinuxContainerExecutor 是启动 docker/runc 容器的**前置条件**：

- `container-executor` 二进制须 `root:hadoop` 属主 + `6050` 权限（SUID 程序，普通用户经它提权启动容器）
- `container-executor.cfg` 须 root 属主 + `0400` 权限（SUID 二进制启动时读取的强制配置，防止普通用户篡改白名单）
- `yarn.nodemanager.container-executor.class` 指向 LCE；`yarn.nodemanager.runtime.linux.allowed-runtimes` 声明允许的运行时集合（default,docker,runc,javasandbox）
- 应用侧通过环境变量声明运行形态：`YARN_CONTAINER_RUNTIME_TYPE=docker|runc`，并配 `YARN_CONTAINER_RUNTIME_DOCKER_*` / `YARN_CONTAINER_RUNTIME_RUNC_*` 系列细粒度参数（镜像、挂载、运行时名等）

> 💡 启用 LCE 后 NM 无法启动，十有八九是 container-executor 二进制属主/权限错误；安全集群必须用 LCE。

### Docker 运行时双层配置

Docker 运行时配置分两层，**必须协同**：

| 层 | 配置文件 | 定位 | 典型键 |
|---|---|---|---|
| 策略层 | yarn-site.xml（NM 侧） | NM 声明允许什么 | `allowed-container-networks`（默认 host,none,bridge）、`default-container-network`（默认 host）、`privileged-containers.allowed`（默认 false）、capabilities 默认白名单、`enable-userremapping`（默认 true）、uid/gid threshold（默认 1） |
| 强制层 | container-executor.cfg `[docker]` 段 | SUID 二进制读取的最终白名单 | `module.enabled`、`docker.allowed.capabilities/devices/networks/ro-mounts/rw-mounts/volume-drivers/runtimes`、`docker.trusted.registries` 等 |

> 💡 yarn-site 只是 NM 侧策略，cfg 才是 SUID 二进制读取的强制白名单——用户请求的挂载/设备若不在 cfg 白名单内直接拒绝，两处必须协同配置。

### 镜像要求与两种启动模式

![[assets/hadoop/diagram-container-runtime-launch-flow.svg]]

*图：容器启动流程与运行时选择决策（default/docker/runc + docker 两种启动模式 + 白名单校验）*

镜像须含应用运行所需环境（MR/Spark 需 JAVA_HOME、HADOOP_* 系列环境变量且版本与集群兼容）。docker 模式有两种启动形态：

- **默认模式（RUN_OVERRIDE_DISABLE 未设或为 false）**：用 YARN 启动脚本**覆盖镜像 CMD**，因此镜像内必须存在 `/bin/bash` 与 `find`；busybox/alpine 类微型镜像缺二者会启动失败
- **ENTRYPOINT 模式（该变量为 true）**：以镜像 ENTRYPOINT **原生形态**运行，不依赖 bash/find；需把变量加入 `yarn.nodemanager.env-whitelist` 与 yarn-env.sh 才能透传进容器环境

> 💡 busybox 类微型镜像在默认模式启动失败（exec: bash not found / find: command not found），可切 ENTRYPOINT 模式规避。

### 容器内用户管理三方案

容器进程以 **NM 主机上的 uid:gid** 启动（非安全模式默认 nobody=99:99），镜像 `/etc/passwd` 中无对应 uid 会启动失败或身份错乱。官方给出三方案：

1. **静态管理（仅测试）**：`usermod -u 99 nobody` 把镜像内用户固化对齐；难扩展、不推荐生产
2. **bind mount /etc/passwd、/etc/group 只读进容器**：覆盖镜像内用户体系；⚠️ 必须 `:ro`，rw 挂载会致主机用户体系不可用
3. **SSSD 集中认证（生产推荐）**：宿主机 sssd-proxy 域，bind mount `/var/lib/sss/pipes` 进容器，容器内装 sssd-client 并配 nsswitch/pam，实现 LDAP/AD 统一认证

> 💡 uid:gid 以 NM 主机为准而非用户名，主机与镜像用户体系必须打通。

### 特权容器安全模型

- 默认**完全禁止**特权容器（privileged-containers.allowed=false）
- 放行后仍要求：镜像带 ENTRYPOINT **且**来源在信任边界内（`docker.trusted.registries`）；特权给 root 级容器能力但**不开放宿主设备访问**
- `docker.privileged-containers.registries` 可做比 trusted 更细的画像级控制，未配置时回退用 trusted.registries
- 本地镜像建议打 `localhost:5000` 或 `local/` 前缀标签，防止误从 Docker Hub 拉取同名镜像

> 💡 特权容器与设备访问是**两级放行**：enabled 开关 + registry 信任边界，缺一不可。

### runC 运行时与 OCI 镜像链路

- runC 按 OCI 规范启动容器（YARN-9014 umbrella，官方标注 **UNSTABLE**）
- 镜像链路：Docker 镜像经 `docker_to_squash.py`（YARN-9564 附脚本）转换为 squashFS layer、config、manifest 三件套 → 上传 HDFS `/runc-root` → 维护 image-tag-to-hash 映射
- NM 侧：插件解析 manifest → 本地化 layer（`layer-mounts-to-keep` 默认 100 个、600s 回收一轮）→ runc 以 overlay/squashFS mount 启动
- 与 Docker 守护进程**解耦**：无 daemon、镜像以 squashFS 只读文件形态经 HDFS 分发，天然免 docker pull

> 💡 runC 明确不支持向容器内 `/tmp`、`/var/tmp` 挂载；bind mount passwd/group 须经 NM 侧 `runc.default-ro-mounts` 注入（仅加 cfg 白名单不够）。

### GPU 资源建模与调度前置

- `resource-types.xml` 声明 `yarn.resource-types=yarn.io/gpu`（单位 G）
- **CapacityScheduler 必须把 `yarn.scheduler.capacity.resource-calculator` 设为 DominantResourceCalculator**（DRF 多维主导资源公平），否则 GPU 调度与隔离不生效——默认资源计算器只认内存/vcore
- 分布式 shell 以 `-container_resources memory-mb,vcores,yarn.io/gpu=N` 请求 GPU

> 💡 只声明资源类型不够——GPU 必须换 DRF 计算器，这是最常见的「配了不生效」根因。

### GPU 设备发现与隔离

- NM 侧 `yarn.nodemanager.resource-plugins=yarn.io/gpu` 开启隔离模块
- **自动发现（auto）**：默认经 `nvidia-smi` 探测（`path-to-discovery-executables` 可指定）；失败或需子集时用 `allowed-gpu-devices` 手动指定 `index:minor_number`（如 `0:0,1:1`，index 与 minor 必须成对）
- **设备隔离**：cgroup devices 控制器按 GPU minor 号做 per-GPU 隔离；需 cgroups `mount=true` + container-executor.cfg `[gpu] module.enabled=true`，且 `[cgroups]` 的 root/hierarchy 与 yarn-site mount-path/hierarchy **一致**
- Docker 场景额外配：`docker.allowed.devices=/dev/nvidia*`、nvidia_driver_<ver> ro-mounts、volume-driver 与 runtimes（v2 用 nvidia）白名单；仅支持 Nvidia GPU

### Node Label 分区模型

![[assets/hadoop/diagram-nodelabel-partition-model.svg]]

*图：Node Label 分区模型（节点组 → 标签映射 → 队列授权 → exclusive/non-exclusive 语义）*

- 每个节点**仅属一个分区**（默认 DEFAULT 分区=空串），集群被切成不相交子集群
- **exclusive 分区**（默认）只接受标签匹配的请求；**non-exclusive 分区**把空闲资源共享给 DEFAULT 请求
- 队列通过 `accessible-node-labels` 声明可访问标签（不配则继承父队列，配空格=仅无标签节点）；各分区容量百分比独立配置，**同层子队列之和须 100**
- `default-node-label-expression` 可收敛无标签请求到指定分区

> 💡 `capacity` 是 DEFAULT 分区占比，`accessible-node-labels.<label>.capacity` 才是标签分区占比，两者独立配置——改标签分区容量时别改错键。

### Node Label 三种映射模式

| 模式 | 维护方 | 机制 | 适用 |
|---|---|---|---|
| centralized（默认） | RM | `yarn rmadmin -replaceLabelsOnNode` 经 RM 接口维护 | 小集群、手动管理 |
| distributed | NM | config/script provider 上报，脚本输出 `NODE_PARTITION:` 行，10 分钟周期 + 2 分钟心跳重同步，支持动态刷新 | 节点自治上报 |
| delegated-centralized | RM | RM 侧 provider 统一拉取，NM 注册时获取 + 30 分钟刷新 | 大规模、安全收敛 |

> 💡 规模大或主机不可信时用 delegated-centralized，避免逐节点暴露映射接口；标签与映射跨 RM 重启自动恢复。

### 容器生命周期与 NM 恢复集成

- 容器化应用日志聚合、HistoryServer 存储与普通应用完全一致
- NM 重启恢复时通过 `/proc` 下 PID 目录验证容器存活（reacquisition）；若 `/proc` 启用 `hidepid=2` 挂载，**必须**以 `gid=yarn` 白名单放行 yarn 组，否则恢复流程看不到容器 PID 目录即判定死亡并杀容器
- Docker 侧支持 `DELAYED_REMOVAL` 排障延迟删除（默认关）

> 💡 hidepid=2 是 NM 重启丢容器的隐蔽元凶，fstab 挂载参数须带 `gid=yarn`。

### Docker Trusted Registry 与凭证

- Docker client 默认从 NM 主机 `$HOME/.docker/config.json` 取凭证；安全仓库凭据落盘 NM **不推荐**（全员共享）
- 私有仓库方案：YARN Service 方式把 registry 以应用部署到 HDFS（NFS gateway 挂载 /hdfs 做存储）或 S3（storage driver），registry 地址遵循 Hadoop Registry DNS 格式
- 镜像源信任由 `docker.trusted.registries` 统一管控，拉取只允许信任边界内来源

> 💡 私有仓库要么每台 NM docker login（全员共享凭据），要么走 YARN Service 托管 registry + 安全分发。

## 常见问题表

| 问题 | 原因 | 解法 | 官方出处 |
|---|---|---|---|
| Docker 容器启动失败，报 cgroup-parent for systemd cgroup should be a valid slice named as xxx.slice（exit 7） | Docker daemon cgroup driver 为 systemd（dockerd --exec-opt native.cgroupdriver=systemd），YARN 用 --cgroup-parent 纳入自身 cgroup 树，仅支持 cgroupfs driver | systemctl show --property=ExecStart docker.service 确认；systemctl edit --full docker.service 改 native.cgroupdriver=cgroupfs，daemon-reload + restart，所有 NM 主机都要改 | DockerContainers.html CGroups configuration Requirements |
| 容器启动报 exec: bash: executable file not found in $PATH（exit 7）或 prelaunch.err 报 find: command not found（exit 127） | 默认模式用 YARN 启动脚本覆盖镜像命令，依赖 /bin/bash 与 find；busybox/alpine 微型镜像未装 | 镜像内置 bash+find；或切 ENTRYPOINT 模式（YARN_CONTAINER_RUNTIME_DOCKER_RUN_OVERRIDE_DISABLE=true 并加入 env-whitelist 与 yarn-env.sh） | DockerContainers.html ENTRYPOINT Support / Requirements when not using ENTRYPOINT |
| 作业启动超时：>10 分钟无进度上报被判 stall，大镜像首次使用明显 | 镜像未缓存时启动瞬间隐式 docker pull，拉取耗时计入任务时间 | 提前在所有 NM 预拉镜像（sudo docker pull library/openjdk:8）预热；镜像入库走内部 registry，避开作业高峰期首拉 | DockerContainers.html Cluster Configuration / Docker Image Requirements |
| 容器以错误用户身份运行或启动失败（用户不存在、权限错乱） | 容器进程用 NM 主机 uid:gid 启动（非安全默认 nobody 99:99），镜像 /etc/passwd 无该 uid 或对应不同用户 | 静态：镜像内 usermod -u 99 nobody（仅测试）；ro bind mount /etc/passwd、/etc/group（勿 rw）；生产用 SSSD proxy 域 + bind mount /var/lib/sss/pipes | DockerContainers.html User Management in Docker Container |
| 特权容器无法启动，或担心危害宿主机 | 特权容器默认全面禁止；放行要求镜像带 ENTRYPOINT 且来源在信任边界内 | cfg 设 docker.privileged-containers.enabled=true + docker.trusted.registries（如 library）；再用 docker.privileged-containers.registries 收窄到具体镜像；特权不含宿主设备访问，要设备走 trusted 镜像 + rw-mount 白名单 | DockerContainers.html Privileged Container Security Consideration |
| 请求的 bind mount 被拒，或容器可读写宿主敏感目录 | 挂载源不在 cfg allowed ro/rw-mounts 白名单（source 须等于白名单目录或其子路径）；或白名单过宽（/,/etc,/run,/home） | 按最小权限收窄（如 /sys/fs/cgroup:/sys/fs/cgroup:ro）；用户侧 YARN_CONTAINER_RUNTIME_DOCKER_MOUNTS 用 source:dest[:ro\|rw]；严禁 rw 挂载 /etc/passwd、/etc/group | DockerContainers.html Using Docker Bind Mounted Volumes |
| NM 重启后运行中的 Docker 容器全部被杀（reacquisition 失败） | /proc 启用 hidepid=2 挂载后，NM 恢复流程看不到容器 PID 目录，判定已死 | fstab /proc 挂载加 gid=yarn：proc /proc proc nosuid,nodev,noexec,hidepid=2,gid=yarn 0 0 | DockerContainers.html Container Reacquisition Requirements |
| GPU 请求不生效：容器拿不到 GPU、调度不感知 yarn.io/gpu | CapacityScheduler 未启用 DominantResourceCalculator；或 NM 未开 resource-plugins=yarn.io/gpu；auto 发现失败且未手动指定 | capacity-scheduler.xml 设 DRF 计算器；yarn-site 开 resource-plugins 且 nvidia-smi 可用；auto 失败按 nvidia-smi -q Minor Number 配 allowed-gpu-devices=index:minor（如 0:0,1:1,2:2,3:4）；cfg 开 [gpu] module.enabled 与 [cgroups] root/hierarchy | UsingGpus.html Configs（GPU scheduling / GPU Isolation） |
| Node Label 配置后调度不生效、标签删不掉、RM 起不来 | 漏开 yarn.node-labels.enabled 或 fs-store.root-dir 未创建且 RM 无权限；改队列配置后未刷新；标签仍被队列关联时删除被拒 | 开 enabled + fs-store.root-dir（HDFS 须 yarn 可读写或 file:/// 落 RM 本地）；改完队列配置 yarn rmadmin -refreshQueues 并在 RM Web UI scheduler 页核对；先解绑队列 accessible-node-labels 再删标签 | NodeLabel.html Configuration（Setting up RM / Remove node labels / Schedulers） |
| runC 容器挂载 /tmp、/var/tmp 后运行时异常；用户体系挂载不生效 | runC 不支持向容器内 /tmp、/var/tmp 挂载；passwd/group bind mount 须 NM 侧 default-ro-mounts 注入 | 避免向 /tmp、/var/tmp 发起挂载；配 yarn.nodemanager.runtime.linux.runc.default-ro-mounts=/etc/passwd:/etc/passwd:ro,/etc/group:/etc/group:ro，runc.allowed.ro-mounts 同步放行 | RuncContainers.html Application Submission 注记 / Using runC Bind Mounted Volumes |

## 最佳实践

### 容器功能安全基线：默认全禁 + 最小白名单

特权容器默认 false、host PID namespace 默认 false、capabilities 收敛到默认集（可设 none 全禁）；container-executor.cfg 只放行业务必需的 ro/rw-mounts、devices、networks、volume-drivers；cfg 文件 root:0400、container-executor 二进制 root:hadoop 6050；启用前通读 Docker security 官方文档。

### 镜像工程化：预拉 + 内部 registry + 版本兼容

大镜像提前 docker pull 预热所有 NM daemon 缓存，避免任务启动隐式拉取超时；本地镜像打 localhost:5000 或 local/ 前缀标签防止误从 Docker Hub 拉取；镜像内固定 JAVA_HOME 与 HADOOP_COMMON_PATH/HDFS_HOME/MAPRED_HOME/YARN_HOME/CONF_DIR 且与集群版本兼容，跨任务镜像版本保持一致。

### 主机-容器用户体系生产方案选 SSSD

官方对比三种用户管理：静态 usermod 与 bind mount /etc/passwd 均标注 not recommended beyond testing（前者难加用户、后者覆盖镜像用户且不可变）；生产环境用 SSSD proxy 域 + bind mount /var/lib/sss/pipes 实现 LDAP/AD 集中认证，容器内仅需 sssd-client 与 nsswitch/pam 配置。

### GPU 特性开启清单化

四件套缺一不可：resource-types.xml 声明 yarn.io/gpu、CapacityScheduler 换 DominantResourceCalculator、yarn-site 开 resource-plugins 且 cgroups mount=true、container-executor.cfg [gpu] module.enabled + [cgroups] root/hierarchy 与 yarn-site 一致；Docker 场景另加 docker.allowed.devices=/dev/nvidia*、nvidia_driver_<ver> ro-mounts、volume-driver 与 runtimes（v2 用 nvidia）白名单；仅支持 Nvidia GPU。

### Node Label 分区治理模式

标签分区默认 exclusive，需共享空闲资源再显式标 non-exclusive；队列访问用 accessible-node-labels 逐队列授权、分区容量用 `<label>.capacity` 精确控比（父下子队列和须 100）、default-node-label-expression 收敛无标签请求；RM 运行中随时 rmadmin 动态调整并用 refreshQueues 生效；大规模集群优先 delegated-centralized 或 distributed provider 支持动态刷新。

### runC 按实验特性管控投产节奏

官方 Security Warning 标注 runC UNSTABLE（API 可能变更）；投产前评估镜像 squashFS 转换 + HDFS 分发全链路（docker_to_squash.py、/runc-root 布局、layer mount 保留 100 个/600s 回收、image-tag-to-hash 缓存刷新 60s）；大镜像 localization 超 10 分钟会被 MR/Spark 判 stall，镜像体积与本地化时间要压测。

## 排查命令

```bash
# ---- LCE 前置检查（NM 无法启动先查这两项） ----
ls -l /path/to/container-executor        # 期望 -rwsr-x--- root:hadoop 6050
ls -l /path/to/container-executor.cfg    # 期望 -r-------- root 0400
# yarn-site.xml
#   yarn.nodemanager.container-executor.class=org.apache.hadoop.yarn.server.nodemanager.LinuxContainerExecutor
#   yarn.nodemanager.runtime.linux.allowed-runtimes=default,docker,runc

# ---- Docker daemon cgroup driver 检查与修复 ----
systemctl show --property=ExecStart docker.service   # 看 native.cgroupdriver
sudo systemctl edit --full docker.service            # 改为 cgroupfs
sudo systemctl daemon-reload && sudo systemctl restart docker.service

# ---- 镜像预热 + 本地标签 ----
sudo docker pull library/openjdk:8                    # 所有 NM 主机预拉
sudo docker tag myimg localhost:5000/myimg:tag        # 本地镜像防误拉 Hub

# ---- 容器内用户对齐（测试用）与 SSSD（生产） ----
# docker exec 进镜像: usermod -u 99 nobody && groupmod -g 99 nobody
# fstab /proc 挂载（NM 重启恢复 + hidepid 共存）:
#   proc /proc proc nosuid,nodev,noexec,hidepid=2,gid=yarn 0 0

# ---- GPU 调度与隔离四件套 ----
# resource-types.xml:  yarn.resource-types=yarn.io/gpu
# capacity-scheduler.xml:
#   yarn.scheduler.capacity.resource-calculator=org.apache.hadoop.yarn.util.resource.DominantResourceCalculator
# yarn-site.xml:
#   yarn.nodemanager.resource-plugins=yarn.io/gpu
#   yarn.nodemanager.resource-plugins.yarn.io/gpu.allowed-gpu-devices=0:0,1:1,2:2,3:4   # index:minor 成对
nvidia-smi -q | grep -E "Minor Number|Product Name"    # 查实际 minor 号
# container-executor.cfg:  [gpu] module.enabled=true
#   [cgroups] root=/sys/fs/cgroup  hierarchy=/sys/fs/cgroup/yarn  (须与 yarn-site 一致)
#   [docker] docker.allowed.devices=/dev/nvidiactl,/dev/nvidia-uvm,/dev/nvidia0,...
#           docker.allowed.ro-mounts=/usr/lib64/nvidia:/usr/lib64/nvidia:ro,...
#           docker.allowed.volume-drivers=nvidia  docker.allowed.runtimes=nvidia

# ---- Node Label 运维 ----
yarn rmadmin -addToClusterNodeLabels "gpu(exclusive),hmem"          # 建标签
yarn rmadmin -replaceLabelsOnNode "node1:50000=gpu node2:50000=gpu" # centralized 映射
yarn rmadmin -removeFromClusterNodeLabels "gpu"                     # 先解绑队列再删
yarn rmadmin -refreshQueues                                        # 队列配置生效
# capacity-scheduler.xml 队列分区示例:
#   yarn.scheduler.capacity.root.a.accessible-node-labels=gpu
#   yarn.scheduler.capacity.root.a.accessible-node-labels.gpu.capacity=50

# ---- runC default-ro-mounts（passwd/group 注入须走 NM 侧） ----
# yarn-site.xml:
#   yarn.nodemanager.runtime.linux.runc.default-ro-mounts=/etc/passwd:/etc/passwd:ro,/etc/group:/etc/group:ro
```

## 相关笔记

- [[hadoop-ecosystem]] — Hadoop 生态全景概述（基础方法论篇）
- [[hadoop-core-internals]] — HDFS/YARN/MR 官方文档深度篇（含 CGroups 基础）
- [[hadoop-yarn-scheduling-ecosystem]] — YARN 调度与容错深度 + 生态工具实战
- [[hadoop-mapreduce-execution-hdfs-storage]] — MR 执行机制 + HDFS 存储体系 + YARN 可扩展资源模型
