---
created: 2026-08-13
topic: Hadoop 生态
subtopic: HDFS 架构、YARN 资源调度、MapReduce 原理及常用生态工具
tags: [hadoop, hdfs, yarn, mapreduce, 大数据, 官方文档]
---

# Hadoop 核心机制官方文档深度笔记

> 本篇为官方文档细节深度篇，与 [[hadoop-ecosystem]]（基础方法论篇）互补。所有结论均出自 Apache Hadoop 官方文档（HdfsDesign / CapacityScheduler / FairScheduler / MapReduceTutorial 等）。

## 概述

Apache Hadoop 是开源分布式计算框架，核心为 **HDFS**（分布式存储）、**YARN**（资源调度）、**MapReduce**（分布式计算）三大组件。官方文档揭示了大量生产级细节：

- **HDFS**：Safemode 启动保护机制、可插拔块放置策略、纠删码（EC）以 50% 以下存储开销替代 3 副本 200% 开销、校验和完整性校验、快照回滚、Diskbalancer 单机磁盘均衡
- **YARN**：调度器通过队列层次、容量保证、弹性与抢占实现多租户共享；CapacityScheduler 面向多租户容量保证，FairScheduler 面向资源公平分享；CGroups 提供容器 CPU 硬隔离
- **MapReduce**：shuffle/sort/reduce 三阶段数据流与内存调优参数（io.sort.mb、spill.percent、memory.mb 与 -Xmx 匹配）

## 架构图

![[assets/hadoop/diagram-hdfs-yarn-mr-arch.svg]]

*图：HDFS + YARN + MapReduce 三层架构（控制流与数据流分离，数据永不经过 NameNode）*

**架构要点**：

- **HDFS 主从架构**：单个 NameNode 管理文件系统命名空间与块→DataNode 映射；DataNode 负责块的实际存储与读写，定期发送心跳与块报告。控制流与数据流分离——数据永不经过 NameNode 传输
- **YARN 拆分**：ResourceManager（含纯调度器 Scheduler 与 ApplicationsManager）为全局仲裁者；NodeManager 为每机代理，负责容器生命周期与资源监控；ApplicationMaster 为每应用一个的框架库，向 Scheduler 协商容器并与 NodeManager 协作执行任务
- **MapReduce on YARN**：MRAppMaster 作为 ApplicationMaster 执行 Mapper/Reducer 子进程；map 输出经分区（Partitioner）后由各 reduce 通过 HTTP 拉取（shuffle），排序分组后归约输出

## 核心概念

### Safemode 安全模式

NameNode 启动时进入的特殊状态。期间**不进行数据块复制**，仅接收 DataNode 的心跳与块报告。每个块有最小副本数要求，当达到可配置比例的已安全复制块上报（另加 30 秒）后，NameNode 退出 Safemode，随后对仍低于副本数的块启动复制。

> 关键点：Safemode 是启动保护机制，可通过 `hdfs dfsadmin -safemode enter/leave` 手动控制；退出条件为「安全复制块比例（dfs.namenode.safemode.threshold-pct 默认 0.999）+ 30 秒」。

### 可插拔块放置策略

除默认 `BlockPlacementPolicyDefault`（副本 3 时：本地机架 1 个 + 远程机架 2 个）外，HDFS 支持 4 种可插拔策略：

| 策略 | 特点 |
|------|------|
| BlockPlacementPolicyRackFaultTolerant | 3 副本放 3 个不同机架，防双机架同时故障 |
| BlockPlacementPolicyWithNodeGroup | 三层拓扑，虚拟化环境下同物理机最多 1 副本 |
| BlockPlacementPolicyWithUpgradeDomain | 滚动升级域维度分布副本 |

> 关键点：通过 `hdfs-site.xml` 的 `dfs.block.replicator.classname` 切换；RackFaultTolerant 解决默认策略只用 2 个机架、双机架故障导致数据不可用的问题。

### HDFS 纠删码（EC）

EC 替代 3 副本复制：默认 3x 复制有 **200% 存储开销**，EC 典型配置开销**不超过 50%**。采用条带化（striping）将文件分成条带单元（cell），对每条带原始数据单元计算校验单元（parity）。

内置 5 种策略：RS-3-2-1024k、RS-6-3-1024k、RS-10-4-1024k、RS-LEGACY-6-3-1024k、XOR-2-1-1024k，默认系统策略 **RS-6-3-1024k**。默认 RS 编解码器可借助 Intel ISA-L 库硬件加速。

> 关键点：EC 策略按目录设置，新建文件继承最近祖先目录策略；RS(6,3) 要求最少 9 个 DataNode、3 个机架（理想 9+），CPU 与网络开销显著增加。

### 数据完整性校验

HDFS 客户端在创建文件时为每个块计算**校验和**，存入同一命名空间的独立隐藏文件。读取时客户端校验收到的数据与校验和是否匹配，不匹配则从另一个持有副本的 DataNode 重新获取。

> 关键点：校验和机制应对存储设备故障、网络故障、软件 bug 导致的数据损坏；`hdfs fsck` 命令可扫描缺失/损坏块。

### CapacityScheduler 容量调度器

专为**多租户共享集群**设计：

- **队列（Queue）是核心抽象**：支持层次队列（子队列先共享组织内资源再对外）
- **容量保证**：每队列分配集群容量比例，软限制 + 可选硬限制
- **弹性**：空闲资源可借给超额队列，需求回归时回收，支持抢占
- **多租户限制**：单应用/用户/队列不可垄断
- **运行时配置**：`yarn rmadmin -refreshQueues` 热更新

容器分配三种触发方式：节点心跳、异步调度（默认）、全局调度。

> 关键点：抢占需开启 `yarn.resourcemanager.scheduler.monitor.enable` + ProportionalCapacityPreemptionPolicy；队列删除须先 STOPPED 且无运行/等待应用；支持百分比/绝对值/权重三种资源配置。

### FairScheduler 公平调度器

让所有应用**平均分享集群资源**：单应用时独占集群，新应用提交后逐步分得资源，短作业快速完成而不饿死长作业。支持内存单资源或 DRF（主导资源公平）多资源公平。

- 队列可配 `minResources`（最小保证）、`maxResources`、权重
- 内置三种策略：FifoPolicy / FairSharePolicy（默认）/ DominantResourceFairnessPolicy
- 分配文件每 10 秒自动重载

> 关键点：队列低于 minResources 时优先获得资源，不需要时多余资源分给其他队列；抢占默认关闭，需 `yarn.scheduler.fair.preemption=true` 且利用率超 0.8 阈值触发。

### CGroups 容器资源隔离

YARN 通过 `LinuxContainerExecutor` + `CGroupsLCEResourcesHandler` 启用 cgroups（v1 自内核 2.6.24，v2 自 4.5），限制容器 CPU 等资源。

- `yarn.nodemanager.resource.percentage-physical-cpu-limit`：所有 YARN 容器累计 CPU 硬上限（如 60%）
- `strict-resource-usage=true`：容器不能超过分配 CPU 即便有空闲

> 关键点：必须同时设置 `container-executor.class=LinuxContainerExecutor` 与 `resources-handler.class=CGroupsLCEResourcesHandler`；cgroup v2 需 `yarn.nodemanager.linux-container-executor.cgroups.v2.enabled=true`，未挂载时自动回退 v1；**YARN 挂载 cgroups 已废弃（安全原因），需系统预先挂载**。

### MapReduce shuffle/sort/reduce 三阶段

Reducer 有 3 个阶段：

1. **shuffle**：框架通过 HTTP 拉取所有 mapper 输出中分配给自己的分区
2. **sort**：按 key 分组（不同 mapper 可能输出相同 key）
3. **reduce**：对每组 key 调用 reduce 方法

shuffle 与 sort **同时进行**——边拉取边合并。可配合 `Job.setSortComparatorClass` 与 `setGroupingComparatorClass` 模拟对值的二次排序。

![[assets/hadoop/diagram-mapreduce-shuffle-sort.svg]]

*图：MapReduce shuffle/sort/reduce 三阶段数据流*

> 关键点：map 输出先写入内存缓冲（`mapreduce.task.io.sort.mb`），超过 `mapreduce.map.sort.spill.percent`（默认 0.8）后台溢写磁盘，阈值是**触发而非阻塞**；reduce 端 map 输出大于 shuffle 内存 25% 时直接写磁盘。

### MapReduce 内存管理

- `mapreduce.map.java.opts` / `reduce.java.opts`：配置子 JVM 参数（-Xmx 等，`@taskid@` 占位符可插值）
- `mapreduce.map.memory.mb` / `reduce.memory.mb`：子任务进程虚拟内存上限（每进程限制，**必须 ≥ -Xmx** 否则 JVM 可能无法启动）

任务由 MRAppMaster 作为独立 JVM 子进程执行。

> 关键点：container 内存超限是任务被杀（KILLED）的常见原因——-Xmx 与 memory.mb 不匹配、物理内存+虚拟内存超限都会触发。

### 快照（Snapshots）与磁盘均衡

- **快照**：目录设 snapshottable 后可最多容纳 **65536 个**并发快照，通过 `.snapshot` 保留路径访问（`/foo/.snapshot/s0/bar`），可用于回滚损坏实例；有快照的目录**不可删除/重命名**，嵌套 snapshottable 目录不允许
- **Diskbalancer**：均衡单个 DataNode 各磁盘数据（区别于集群级 Balancer），生成 plan 后异步执行，默认开启，`-bandwidth` 限速、`-thresholdPercentage` 容差

> 关键点：`snapshotDiff` 可对比两个快照差异（+/-/M/R）；diskbalancer 适用于大量写入删除或换盘导致单机磁盘倾斜场景。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方出处 |
|------|------|----------|----------|
| NameNode 长时间处于 Safemode 或异常进入，集群只读 | 安全复制块比例未达阈值（threshold-pct 默认 0.999）；DataNode 大量掉线/块报告延迟；管理员手动进入 | `hdfs dfsadmin -safemode get` 查看状态；等待上报；必要时 `-safemode leave` 强制退出（需确认数据完整） | HdfsDesign Safemode 章节 |
| DataNode 故障/网络分区后部分块副本数低于期望 | 心跳超时被标记 dead（默认超时保守地长于 10 分钟，避免状态抖动引发复制风暴） | NameNode 自动 re-replication；缩短 stale 判定间隔；`hdfs fsck -replicate` 手动触发复制 | HdfsDesign Heartbeats and Re-Replication + HDFSCommands fsck |
| 读取数据校验失败（数据损坏） | 存储设备/网络故障或软件 bug 导致块与校验和不匹配 | 客户端自动从其他副本 DataNode 重新获取；`hdfs fsck -list-corruptfileblocks` 列出，`-move` 移至 /lost+found，`-delete` 删除 | HdfsDesign Data Integrity + HDFSCommands fsck |
| 集群数据分布不均，部分 DataNode 磁盘使用率过高 | 大量写入删除、新增节点、节点替换导致块分布偏离均衡 | `hdfs balancer` 集群级均衡（-threshold 默认 10%）；`hdfs diskbalancer -plan/-execute` 单节点内跨磁盘均衡，-bandwidth 限速 | HDFSCommands balancer + HDFSDiskbalancer |
| 未配置机架感知，所有副本集中同一机架 | 未设置 net.topology.script.file.name，所有节点返回 /default-rack | 配置拓扑脚本（实现 DNSToSwitchMapping，输出 /myrack/myhost）；脚本批量接收 IP（number.args 默认 100） | RackAwareness + HdfsDesign Replica Placement |
| EC 集群配置不当：DataNode 数或机架数不足导致容错失效 | 条带化要求 DataNode ≥ 条带宽度（RS-6-3 需 9+）；机架级容错要求机架 ≥ (数据+校验)/校验 | `hdfs ec -verifyClusterSetup` 校验；按规模选策略（9 机架用 RS-6-3，14+ DataNode 仅需节点级容错用 RS-10-4）；`hdfs ec -enablePolicy` | HDFSErasureCoding Cluster and hardware configuration |
| CapacityScheduler 队列容量被超额占用，低容量队列应用饥饿 | 弹性机制允许借用空闲资源，超额队列不主动归还；抢占默认关闭 | 开启 `yarn.resourcemanager.scheduler.monitor.enable=true` + ProportionalCapacityPreemptionPolicy；调 max_wait_before_kill（默认 15s）；可先 observe_only=true 观察 | CapacityScheduler container preemption 章节 |
| FairScheduler 下长作业被饿死或最小保证不生效 | minResources 只在未达最小份额时优先；抢占默认关闭且超时默认 Long.MAX_VALUE | 设置 minSharePreemptionTimeout / fairSharePreemptionTimeout 与 fairSharePreemptionThreshold（默认 0.5）；开启 preemption 且利用率超 0.8 触发 | FairScheduler Allocation file format |
| MapReduce 容器被 NodeManager 杀死（KILLED/内存超限） | memory.mb 与 -Xmx 不匹配（memory.mb 必须 ≥ -Xmx），或容器实际内存/虚拟内存超限 | 统一调大 java.opts 与 memory.mb 并保持关系；`yarn logs -applicationId` 查日志；调 shuffle.input.buffer.percent 减 reduce 内存压力 | MapReduceTutorial Memory Management + YarnCommands logs |
| 快照目录无法删除或重命名 | snapshottable 目录下存在快照时禁止删除/重命名；嵌套 snapshottable 不允许 | 先 `hdfs dfs -deleteSnapshot` 删除所有快照再删/改名；升级旧版本前先重命名或删除 .snapshot 路径 | HdfsSnapshots Snapshottable Directories |

## 最佳实践

### 机架感知副本放置

副本 3 时：本地机架 1 副本 + 远程机架 2 副本，兼顾写性能（减少跨机架流量）、可靠性（容机架故障）与读性能（就近读副本）。超过 3 副本时随机放置并保证每机架不超过 `(replicas-1)/racks+2` 上限。双机架同时故障风险高的环境换用 BlockPlacementPolicyRackFaultTolerant。

### NameNode 元数据多重副本 + QJM HA

FsImage 与 EditLog 是 HDFS 核心数据结构，损坏即实例不可用。配置多份副本同步更新（牺牲少量事务吞吐）。生产环境启用 HA：**QJM**（分布式 edit log，JournalNode 仲裁）为推荐方案，优于 NFS 共享存储。

### 按集群规模选择 EC 策略

默认系统策略 RS-6-3-1024k；RS(6,3) 需 9+ DataNode、3+ 机架；仅需节点级容错且 DataNode ≥ 14 时可用 RS-10-4-1024k（存储效率更高）；启用 Intel ISA-L 加速编解码（`hadoop checknative` 验证）。**EC 只对新文件生效**（按目录设置），迁移需重写数据。

### 善用快照做安全回滚

对关键目录（如 Hive 数仓根目录）设置 snapshottable 并周期性 createSnapshot，损坏或误删时通过 `.snapshot` 路径恢复；snapshotDiff 审计变更。注意 65536 上限与「有快照的目录不可删/改名」。

### CapacityScheduler 运行时队列管理

队列配置通过 capacity-scheduler.xml + `yarn rmadmin -refreshQueues` 热更新（无需重启 RM）；删除队列前须将队列 STOPPED 且无运行/等待应用，先排空再删；可用 `yarn.resourcemanager.queue.auto.refresh.monitoring-interval` 启用周期性自动刷新；用 Scheduler/Application Activities REST API 诊断调度决策。

### FairScheduler 为关键队列设最小保证

生产队列配 minResources 保证最低资源，配 maxRunningApps 限制并发、maxAMShare（默认 0.5）防止 AM 挤占任务资源；利用 placement rules 按用户/组自动归队；队列用 weight 控制非等比分享。

### CGroups 严格限制容器 CPU

多租户共享集群用 LinuxContainerExecutor + cgroups 隔离 CPU：percentage-physical-cpu-limit 设所有容器累计硬上限，strict-resource-usage=true 保证容器只能用分配到的 CPU（隔离更严格，牺牲空闲 CPU 利用率）；cgroup v2 环境显式开启 v2 支持。

### MapReduce 作业提交携带依赖

作业依赖的脚本/文件用 `-files` 分发到任务工作目录，jar 用 `-libjars` 加入 map/reduce classpath，归档用 `-archives` 自动解压；开启推测执行（speculative）让慢任务由备份任务加速，配 maxMapAttempts/maxReduceAttempts 控制失败重试上限；`mapred job -history` 查看失败任务明细。

## 排查命令

```bash
# Safemode 状态与控制
hdfs dfsadmin -safemode get          # 查看当前状态
hdfs dfsadmin -safemode leave        # 强制退出（确认数据完整后）

# 文件系统健康检查
hdfs fsck /                          # 扫描缺失/损坏/错配副本块
hdfs fsck -list-corruptfileblocks    # 列出损坏块
hdfs fsck -replicate                 # 触发复制使错配块满足放置策略
hdfs fsck -move                      # 损坏文件移至 /lost+found

# 数据均衡
hdfs balancer -threshold 10          # 集群级均衡，10% 容差
hdfs diskbalancer -plan <dn-host>    # 单节点磁盘均衡：生成 plan
hdfs diskbalancer -execute <plan>    # 执行 plan（-bandwidth 限速）

# EC 纠删码
hdfs ec -verifyClusterSetup          # 校验集群是否满足 EC 策略要求
hdfs ec -enablePolicy RS-6-3-1024k   # 启用策略
hadoop checknative                   # 验证 ISA-L 硬件加速

# 调度器热更新
yarn rmadmin -refreshQueues          # 重载 CapacityScheduler 队列配置

# 作业与容器排查
yarn logs -applicationId <app-id>    # 查看容器日志（确认内存超限）
mapred job -history <job-file>       # 查看失败任务明细

# 快照管理
hdfs dfs -createSnapshot <dir> <name>
hdfs dfs -deleteSnapshot <dir> <name>
hdfs snapshotDiff <dir> <s1> <s2>    # 对比两个快照差异（+/-/M/R）
```

## 相关笔记

- [[hadoop-ecosystem]] — Hadoop 生态基础方法论篇（组件全景、入门概念），本篇为其官方文档细节深度补充
- [[k8s-cluster-ops-combat]] — 云原生资源调度对照（YARN 调度器与 K8s Scheduler 的队列/抢占思想可类比）
