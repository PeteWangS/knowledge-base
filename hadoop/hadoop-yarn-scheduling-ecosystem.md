---
created: 2026-08-20
topic: Hadoop 生态
subtopic: YARN 资源调度与容错深度 + 常用生态工具实战
tags: [hadoop, yarn, capacity-scheduler, distcp, streaming, federation, 官方文档]
---

# YARN 调度与容错深度 + 生态工具实战笔记

> 本篇为 Hadoop 生态第三轮研究，取「YARN 资源调度与容错深度 + 常用生态工具实战」新角度，与 [[hadoop-ecosystem]]（基础方法论篇）、[[hadoop-core-internals]]（HDFS/YARN/MR 官方文档深度篇）互补。内容出自 Apache Hadoop 官方文档（CapacityScheduler / ResourceManagerRestart / GracefulDecommission / ReservationSystem / Federation / DistCp / HadoopStreaming / HadoopArchives / SLS / ResourceEstimator / Dynamometer）。

## 概述

YARN 作为 Hadoop 的资源调度与作业编排层，其**调度能力**与**容错能力**决定集群的利用率与稳定性；**生态工具**则解决数据复制、流式接入、小文件治理与调优验证等日常问题。本轮要点：

- **调度**：CapacityScheduler 四种资源配置模式（百分比 / 绝对容量 / 权重 / Universal Capacity Vector）、队列映射与动态自动建队列、应用优先级（仅 FIFO 排序生效，优先级 ≠ 抢占）
- **容错**：RM 重启恢复（工作保全 vs 非工作保全）、state-store 三种实现与 fencing 差异、节点优雅下线状态机（DECOMMISSIONING 六种子状态）、ReservationSystem 预留八步流程、YARN Federation 联邦架构四组件
- **生态工具**：DistCp（-update / -delete / -atomic / -diff 快照增量）、Hadoop Streaming（stdin/stdout 协议与排错）、Hadoop Archive / ArchiveLogs 小文件治理、SLS 调度模拟器、ResourceEstimator 资源预估、Dynamometer NameNode 压测

## 架构图

![[assets/hadoop/diagram-yarn-scheduling-fault-tolerance-arch.svg]]

*图：YARN 调度与容错整体架构（RM 内部组件 + 可插拔 state-store + Federation 联邦层 + 生态工具执行引擎）*

**架构要点**：

- **ResourceManager 是全局仲裁者**：内部 Scheduler 为纯调度器（可替换为 Capacity / Fair / FIFO），ApplicationsManager 管理应用生命周期，NodesListManager 依据 exclude/include 文件驱动节点生命周期，DecommissioningNodeWatcher 每 20 秒轮询跟踪优雅下线节点
- **持久化状态可插拔**：state-store 有 ZK / FileSystem / LevelDB 三种实现，工作保全恢复时 RM 结合 NM 上报的容器状态与 AM 重发的资源请求重建调度器运行态
- **Federation 是叠加层**：Router 统一接收作业提交并按策略路由到子集群 RM，AMRMProxy 运行于每个 NM 上代理 AM 与多个 RM 的通信，GPG 离线路由策略（不可用不影响集群运转），State-Store 记录子集群成员与应用 home 映射
- **生态工具以 MapReduce 为执行引擎**：DistCp 的文件清单 SequenceFile + 动态/均匀大小分片、Dynamometer 在 YARN 应用内起真实 NameNode + SimulatedFSDataset 模拟 DataNode

## 核心概念

### CapacityScheduler 四种资源配置模式

队列容量既可用**百分比**（capacity），也可用**绝对资源量**（absoluteCapacity，如 memory-mb / vcores 具体数值）、**权重**（weight，动态层级变更下自适应）或 **Universal Capacity Vector**（每种资源类型可分别用绝对 / 权重 / 百分比混合指定）配置，后三者提供比百分比更精确的控制。

> 关键点：资源型调度目前 memory 为基本需求维度；百分比模式在层级动态变化时可能失真，权重模式更适合频繁调整的队列层级。

### 队列映射与动态自动建队列

Queue Mapping Interface 支持按用户 / 组或应用名等默认放置规则把作业映射到指定队列，也可自定义放置规则；动态 Auto-Creation 与 queue-mapping 配合，可基于 user-group 映射自动创建叶子队列，父队列上配置策略管理这些自动队列的容量。

> 关键点：自动建队列省去管理员手动建队列；但队列删除仍须先 STOPPED 且无运行/等待应用（运行时只能增不能随意删）。

### 应用优先级调度（Priority Scheduling）

应用可带不同优先级提交与调度，整数越大优先级越高；当前优先级仅在 **FIFO 排序策略**下生效，且可用 `yarn application -updatePriority` 运行时调整。

> 关键点：优先级 ≠ 抢占：优先级只影响 FIFO 队列内的排序，不自动触发资源抢占。

### RM 重启恢复（工作保全 vs 非工作保全）

- **非工作保全重启**：RM 把应用元数据（ApplicationSubmissionContext）与凭据存入 state-store，重启后重建 AM 重新拉起应用，但运行中工作丢失（re-sync 命令让 NM 杀容器）
- **工作保全重启**：RM 结合 NM 上报的容器状态与 AM 重发的资源请求重建调度器运行态，AM 不被杀、应用从断点继续

> 关键点：工作保全下容器不丢，AM 需重发未满足的资源请求（AMRMClient 自动处理）；containerId 格式增加 epoch 前缀（如 Container_e17_...），RM 每次重启 epoch 递增。

### state-store 三种实现与 fencing

| 实现 | 特点 | 支持 HA |
|------|------|---------|
| ZKRMStateStore（ZooKeeper） | 唯一支持 fencing，防 split-brain | ✅ |
| FileSystemRMStateStore（HDFS / 本地 FS，默认） | 简单，无 fencing | ❌ |
| LeveldbRMStateStore | 最轻量、原子操作好、文件少 | ❌ |

> 关键点：启用 RM 重启需 `yarn.resourcemanager.recovery.enabled=true` + 指定 store.class；要支持 RM HA 必须选 ZK state-store。

### 节点优雅下线（Graceful Decommission）

`yarn rmadmin -refreshNodes [-g 超时 -client|server]` 触发：节点进入 **DECOMMISSIONING** 后 RM 不再为其调度新容器，等待运行中容器与应用完成（或超时）后转 DECOMMISSIONED 并通知 NM 关闭。DecommissioningNodeWatcher 每 20 秒轮询心跳跟踪，子状态：NONE → WAIT_CONTAINER → WAIT_APP → TIMEOUT → READY → DECOMMISSIONED。

> 关键点：MR 场景 reducer 需拉取 map 输出，容器完成后节点仍可能处于 WAIT_APP；超时（默认 3600s，-1 无限）保证节点不会无限滞留；XML 格式 exclude 文件支持 per-node timeout；**超时不持久化**，RM 重启 / HA 切换后节点会被立即下线（YARN-5464）。

### ReservationSystem 预留系统

用户用 **RDL**（Reservation Definition Language）描述随时间变化的资源需求与期限，ReservationAgent 在 Plan 中寻找可行分配，SharingPolicy 校验不变量（CapacityOvertimePolicy 可限制瞬时最大容量与周期积分容量，如瞬时 50% 但 24h 平均不超 10%），PlanFollower 在预约时刻动态创建 / 调整队列，作业携带 ReservationId 提交到可预约队列即获资源保证。

> 关键点：预约保证的是**绝对资源量**而非集群百分比；容量下降时系统会重规划迁移预约或拒绝最小量的已接受预约；无预约作业在可预约队列中按 best-effort 运行剩余容量。

### YARN Federation 联邦架构

把 10-100k 节点集群划分为多个子集群（每个仍是完整 YARN，跑工作保全 HA）：

- **Router**：统一接收作业提交并按策略路由到 home 子集群（无状态可恢复）
- **AMRMProxy**：运行于所有 NM，代理 AM 与多个 RM 通信（防 DDoS、屏蔽多 RM、强制配额、跨子集群转发）
- **GPG**：全局策略生成器，离线路由与容量映射（不可用不影响集群运转）
- **State-Store**：存子集群成员心跳与应用 home 映射

> 关键点：子集群是伸缩单位，可部分承诺容量给联邦；AM 通过 AMRMProxy 可跨子集群申请容器（secondary sub-clusters），各子集群用各自安全令牌避免全局共享密钥。

### DistCp 分布式复制

基于 MapReduce 的大规模跨 / 内集群复制工具：Driver 解析参数、Copy-listing generator 把源路径展开成 SequenceFile 文件清单、Map 任务并行复制分片。关键选项：

| 选项 | 作用 |
|------|------|
| `-update` | 按大小 / 块大小 / 校验和差异覆盖 |
| `-overwrite` | 无条件覆盖 |
| `-delete` | 删除目标多余文件（走 trash） |
| `-append` | 增量追加 |
| `-atomic` | 临时目录 + 原子改名提交 |
| `-bandwidth` | 每 map 限速 MB/s |
| `-diff / -rdiff` | 快照差异增量同步 |
| `-blocksperchunk` | 大文件分块并行传输 |

> 关键点：-update / -overwrite 与已存在目标目录配合时复制的是**源目录内容**而非源目录本身；最小工作单元是一个文件（一个文件只由一个 map 处理），map 数超过文件数无收益；-pr 只对非 EC 目录有效；-atomic 的 tmp 目录必须在目标集群。

### Hadoop Streaming 流式接口

允许任意可执行程序（shell / Python / Perl）作为 Mapper / Reducer：框架把输入转成行喂给进程 stdin，从 stdout 收集输出，默认行首到第一个 tab 为 key、其余为 value。可指定 -inputformat / -outputformat / -partitioner / -combiner、`-file` 打包可执行文件与辅助文件随作业分发、`-cmdenv` 传环境变量、`-numReduceTasks` 控制 reducer 数。

> 关键点：非零退出码默认视为任务失败（stream.non.zero.exit.is.failure 可改）；generic 选项必须在 streaming 选项**之前**；计数器 / 状态通过 stderr 的 `reporter:counter:` / `reporter:status:` 协议上报；job 配置参数进环境变量时点号变下划线（mapreduce.job.id → mapreduce_job_id）。

### Hadoop Archive 与 ArchiveLogs 小文件治理

- **Hadoop Archive**：把大量小文件打包成 `.har` 目录（含 _index / _masterindex 元数据与 part-* 数据文件），暴露为只读文件系统层（`har://` URI），改名 / 删除 / 创建会报错；解归档即复制
- **ArchiveLogs**：把 YARN 聚合日志按应用打包成 HAR，减少小文件数量、降低 NameNode 压力，归档后 Job History Server 与 `yarn logs` 仍可读取

> 关键点：归档是 MapReduce 作业，需要集群执行；har 文件在加密 zone 内外按明文 / 密文处理；ArchiveLogs 只处理聚合完成、未归档、日志文件数 ≥ minNumberLogFiles（默认 20）的应用，且集群同时只允许一个实例运行。

### SLS / ResourceEstimator / Dynamometer 调优工具族

- **SLS**（Scheduler Load Simulator）：单机用真实 job trace（Rumen 生成）或 SYNTH 合成负载驱动真实 ResourceManager 调度器，输出实时 / 离线指标评估调度算法
- **ResourceEstimator**：从历史 job 日志提取 ResourceSkyline（RLESparseResourceAllocation），用 LPSOLVER 线性规划预测周期性作业资源需求并自动做 YARN 预留（REST 服务，端口 9998）
- **Dynamometer**：用生产 FsImage + audit log 在 YARN 应用内起真实 NameNode + SimulatedFSDataset 模拟 DataNode，回放真实工作负载做 NameNode 压测（blockgen 作业先用 Offline Image Viewer 把 FsImage 转 XML 提取块元数据）

> 关键点：三者共同思路——用真实生产数据（job trace / FsImage / audit log）在受控环境验证调度器、预估资源、压测 NameNode，避免直接在大型生产集群上试验。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方出处 |
|------|------|----------|----------|
| RM 重启后所有运行中应用被杀死并从头重跑，作业进度丢失 | 只启用非工作保全重启或未启用恢复：RM 重启时向 NM 发 re-sync，NM 杀掉全部容器，AM 退出后 RM 从 state-store 重新拉起新 AM attempt | 启用工作保全恢复：`yarn.resourcemanager.recovery.enabled=true` + `yarn.resourcemanager.work-preserving-recovery.scheduling-wait-ms` 给 RM 重连窗口；NM 在 re-sync 时不再杀容器，AM 通过 AMRMClient 自动重发未满足的资源请求 | ResourceManagerRestart 文档 Work-preserving vs Non-work-preserving 章节 |
| RM HA 故障切换后出现双主或状态错乱（split-brain） | state-store 用了 FileSystem 或 LevelDB 实现——两者均不支持 fencing，无法阻止多个 RM 同时认为自己是 active 并写入状态库 | HA 场景必须改用 ZKRMStateStore（`yarn.resourcemanager.store.class=org.apache.hadoop.yarn.server.resourcemanager.recovery.ZKRMStateStore`），并配置 zk-address 与 parent-path；单 RM 无 HA 需求时可用默认 FileSystemRMStateStore | ResourceManagerRestart 文档 How to choose the state-store implementation 章节 |
| 节点下线长时间停留在 DECOMMISSIONING 状态，缩容迟迟不生效 | 优雅下线要等运行中容器与应用全部完成：MR reducer 需从节点拉 map 输出，容器完成后节点仍处于 WAIT_APP 子状态；未配置超时时默认 3600 秒 | `yarn rmadmin -refreshNodes -g <timeout> -server` 指定超时（-1 无限）；XML exclude 文件按节点单独配 timeout；查 RM 日志每 20 秒的 decommissioning 子状态判断卡点 | GracefulDecommission 文档 Features 章节 + Configuration 表 |
| RM 重启 / HA 切换后，原本优雅下线的节点被立即强制下线 | 优雅下线超时不持久化：server-side 跟踪下 RM 重启后 DecommissioningNodeWatcher 不再知道剩余超时，节点被直接判定超时下线（YARN-5464） | 下线窗口内避免 RM 重启 / HA 切换；或改用 client-side 跟踪（`-client`，阻塞式，超时由客户端守护）；关注 YARN-5464 修复 | GracefulDecommission Client or server side timeout 章节 + YarnCommands rmadmin Known Issue |
| DistCp -update 同步后目标目录结构不符合预期，或海量小文件复制时客户端 OOM | -update / -overwrite 与已存在目标目录配合时复制**源目录内容**而非源目录本身；百万级路径时客户端构建文件清单耗尽堆内存；map 数超过文件总数也不会更快 | 先读 Usage 的 update / overwrite 语义再决定 -p 参数；海量路径用 `HADOOP_CLIENT_OPTS=-Xms64m -Xmx1024m` 增大客户端堆；map 数按文件数设置；复制后生成源 / 目标清单交叉核对，或加 -update 二次校验 | DistCp 文档 Frequently Asked Questions 第 1/4/5 条 |
| Streaming 作业报 No space left on device 或 error=7, Argument list too long | 前者：-file 分发超大可执行文件（如 3.6GB）时 jar 打包在 stream.tmpdir（默认 /tmp）导致空间不足；后者：作业把整个 job 配置复制进环境变量，输入文件极多时超限 | 前者：`-D stream.tmpdir=/export/bigspace/...` 指向大分区；后者：`-D stream.jobconf.truncate.limit=20000` 截断环境变量中的配置副本（0 只复制名称，-1 为不截断） | HadoopStreaming 文档 FAQ（No space left on device / Argument list too long 条目） |
| Streaming -mapper 用 UNIX 管道组合命令报 Broken pipe | `-mapper "cut -f1 \| sed s/foo/bar/g"` 这类管道写法当前不受支持（官方标注待调查 bug）；shell alias 也不生效 | 把组合逻辑写成独立脚本并用 `-file` 分发（如 map.sh 内含管道），`-mapper map.sh`；变量替换可用（c2='cut -f2'；-mapper "$c2"） | HadoopStreaming 文档 FAQ（UNIX pipes / alias 条目） |

## 最佳实践

1. **生产集群启用 RM 工作保全重启恢复**：`yarn.resourcemanager.recovery.enabled=true` 并把 state-store 放在 ZK（HA 必备，唯一支持 fencing 的实现）；配置 work-preserving recovery 的 scheduling-wait-ms 让 RM 重启后先与全部 NM 重连再分配新容器，把 RM 停机对用户透明化，避免作业整批重跑。（官方出处：ResourceManagerRestart Feature + Sample Configurations）
2. **节点维护 / 缩容一律走优雅下线**：`yarn rmadmin -refreshNodes -g <timeout> -server` 触发，XML exclude 文件按节点差异化设置超时（关键节点 -1 无限等待、普通节点短超时）；下线窗口内避免 RM 重启 / HA 切换（超时不持久化）；监控 RM 日志的 decommissioning 子状态定位卡点。（官方出处：GracefulDecommission Features + Configuration）
3. **CapacityScheduler 用绝对容量 / 权重 / Universal Capacity Vector 精确管控队列**：层级频繁调整的队列用 weight 而非百分比；需要精确资源保证的队列用 absoluteCapacity 指定具体 memory / vcores；混合场景逐资源类型用 UCV；配合队列映射 + 动态自动建叶子队列减少运维，运行时用 `yarn rmadmin -refreshQueues` 热更新。（官方出处：CapacityScheduler Features）
4. **DistCp 大数据复制四件套：update + delete + bandwidth + atomic**：周期性同步用 `-update` + `-delete` 保持两端一致；跨机房限 `-bandwidth` 每 map 限速；关键目录用 `-atomic` 临时目录原子提交；快照场景用 `-diff / -rdiff` 只传差异；超大单文件用 `-blocksperchunk` 分块并行。（官方出处：DistCp Command Line Options 表）
5. **小文件治理：Hadoop Archive + ArchiveLogs 双管齐下**：历史小文件目录用 `hadoop archive` 打包成 .har（归档后原文件需自行删除，NameNode 命名空间压力才真正下降）；YARN 聚合日志用 `mapred archive-logs` 按应用打包 HAR，归档后 `yarn logs` 与 Job History Server 仍可正常读取。（官方出处：HadoopArchives + HadoopArchiveLogs）
6. **调度器与 NameNode 变更先用模拟器 / 压测工具验证**：调度器参数上线前用 SLS 加载真实 job trace（Rumen 从 job history 生成）验证吞吐、公平性与容量保证；周期性作业资源预估交给 ResourceEstimator（历史日志学习 + LP 求解 + 自动预约）；NameNode 扩容 / 调参前用 Dynamometer 以生产 FsImage + audit log 回放压测。（官方出处：SLS + ResourceEstimator + Dynamometer）

## 排查命令

```bash
# RM 重启恢复与状态库
yarn rmadmin -getAllResourceProfiles                      # 查看资源 profile
# 启用工作保全恢复（yarn-site.xml）
#   yarn.resourcemanager.recovery.enabled=true
#   yarn.resourcemanager.work-preserving-recovery.enabled=true
#   yarn.resourcemanager.work-preserving-recovery.scheduling-wait-ms=10000
#   yarn.resourcemanager.store.class=...ZKRMStateStore   # HA 必选 ZK

# 节点优雅下线（XML exclude 文件 + 刷新）
yarn rmadmin -refreshNodes -g 3600 -server                # server 侧跟踪，超时 3600s
yarn rmadmin -refreshNodes -g -1 -client                  # client 侧跟踪，无限等待
# exclude 文件（XML 格式支持 per-node timeout）：
#   <host name="host2" timeout="123"/><host name="host3" timeout="-1"/>
yarn node -list -states DECOMMISSIONING                   # 查看下线中节点

# 应用与队列管理
yarn application -list                                    # 列出应用
yarn application -updatePriority <appId> 5                # 运行时调整优先级
yarn rmadmin -refreshQueues                               # 热更新队列配置
yarn top                                                   # 实时资源视图

# DistCp 复制
hadoop distcp -update -delete -bandwidth 100 \
  hdfs://src/dir hdfs://dst/dir                            # 增量同步 + 限速
hadoop distcp -atomic -tmp /tmp/distcp-staging \
  hdfs://src/bigfile hdfs://dst/                           # 原子提交
HADOOP_CLIENT_OPTS="-Xms64m -Xmx1024m" hadoop distcp ...   # 海量路径防客户端 OOM

# Hadoop Streaming
hadoop jar hadoop-streaming.jar \
  -D stream.tmpdir=/export/bigspace                        # -file 大文件防 /tmp 爆盘
  -D stream.jobconf.truncate.limit=20000                   # 防 Argument list too long
  -file map.sh -mapper map.sh -file red.py -reducer red.py \
  -input /in -output /out

# 小文件治理
hadoop archive -archiveName logs.har -p /src/logs /dst     # 打包 .har 归档
mapred archive-logs -harRoot /archive-logs -logRoot /app-logs -appId app_xxx   # YARN 日志归档

# 模拟与压测
# SLS：java -jar hadoop-sls.jar --input-sls <jobtrace.json> --output-dir /sls-out
# ResourceEstimator：REST 服务端口 9998，POST /resourceEstimatorService/estimate
# Dynamometer：blockgen（FsImage→XML 提取块元数据）→ namenode（压测应用）
```

## 相关笔记

- [[hadoop-ecosystem]] — 基础方法论篇：Hadoop 三组件整体认知、部署与基础运维
- [[hadoop-core-internals]] — 官方文档深度篇：HDFS Safemode/EC、YARN 调度器机制、MapReduce shuffle 调优

> 三篇定位：方法论 → 核心机制 → 调度容错与生态工具实战，由浅入深互补阅读。
