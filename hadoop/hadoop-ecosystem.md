# Hadoop 生态核心架构笔记

> 主题：Hadoop 生态 — HDFS 架构、YARN 资源调度、MapReduce 原理及常用生态工具
> 归档日期：2026-07-31 ｜ 来源：Apache Hadoop 官方文档研究

## 概述

Apache Hadoop 是一个开源分布式计算框架，专为在商用硬件集群上存储和处理大规模数据集而设计。核心由三大组件构成：

- **HDFS**（分布式文件系统）：提供高容错性的数据存储
- **YARN**（资源管理器）：负责集群资源管理和作业调度
- **MapReduce**（分布式计算模型）：实现大规模数据的并行处理

此外，Hadoop 生态圈还包括大量附属工具，如 DistCp（跨集群复制）、Hadoop Archive（文件归档）、GridMix（基准测试）、SLS（调度负载模拟器）等。

**设计核心原则**：硬件故障是常态而非例外；流式数据访问优于低延迟交互；大规模数据集优先；简单的写一次读多次一致性模型；移动计算比移动数据更经济。

## 总体架构

Hadoop 采用主从架构，HDFS 与 YARN 双层协同：

![[assets/hadoop/diagram-hadoop-architecture.svg]]

- **HDFS 层**：单个 NameNode（主节点，管理文件系统命名空间和元数据）+ 多个 DataNode（从节点，负责数据块的实际存储和读写）
- **YARN 层**：ResourceManager（全局资源管理主节点，含 Scheduler 和 ApplicationsManager）+ NodeManager（每节点代理，管理容器生命周期和资源监控）+ ApplicationMaster（每个应用一个，负责与 RM 协商容器并与 NM 协作执行任务）
- NameNode 通过 EditLog（事务日志）记录所有元数据变更，通过 FsImage（文件系统镜像文件）持久化命名空间状态
- HDFS 默认数据块大小 128MB，默认副本因子 3

## 核心概念

### 1. HDFS 架构与数据块

HDFS 将文件切分为固定大小的块（默认 128 MB），每个块在集群中多个 DataNode 上复制存储（默认 3 副本）。NameNode 维护文件系统命名空间和块到 DataNode 的映射；DataNode 负责块的实际读写和复制，定时发送心跳和块报告给 NameNode。

> 关键点：单 NameNode 架构简化了设计，但也是 SPOF（可通过 HA 解决）；数据永不经过 NameNode 传输，实现控制流与数据流分离。

### 2. 副本放置策略

默认副本因子为 3 时的放置规则：

1. 第一个副本放在客户端所在节点（或同机架随机节点）
2. 第二个副本放在远程机架的一个节点上
3. 第三个副本放在与第二个副本同远程机架的另一个节点上

超过 3 个副本时后续副本随机放置，同时保证每个机架的副本数不超过上限 `(replicas - 1) / racks + 2`。

> 关键点：此策略在写性能（减少跨机架流量）、数据可靠性（容机架故障）和读性能（最大带宽利用）之间取得平衡。

### 3. EditLog 与 FsImage 检查点机制

NameNode 将所有元数据变更以记录形式追加到 EditLog。周期性 checkpoint 将 EditLog 中的事务应用到内存中的 FsImage，然后刷写到磁盘生成新 FsImage 并截断旧 EditLog。checkpoint 由时间间隔（`dfs.namenode.checkpoint.period`，默认 3600 秒）或事务数量（`dfs.namenode.checkpoint.txns`，默认 1,000,000）触发。

> 关键点：元数据变更写入 EditLog 效率高；checkpoint 确保一致性；HA 集群中 Standby NameNode 同时执行 checkpoint 职责，无需独立的 Secondary NameNode。

### 4. YARN 资源容器模型

YARN 以 Container（容器）为基本资源分配单元，每个 Container 包含 CPU（vcores）、内存（memory-mb）等资源。ApplicationMaster 为作业中的每个任务向 ResourceManager Scheduler 请求容器；NodeManager 负责启动、监控和清理容器。

> 关键点：Container 是抽象资源单位，支持内存、CPU、磁盘、网络等多维度资源调度；FairScheduler 支持 Dominant Resource Fairness（DRF）实现多资源公平调度。

### 5. CapacityScheduler 与 FairScheduler

| 特性 | CapacityScheduler | FairScheduler |
|------|-------------------|---------------|
| 分配单位 | 队列，每个队列有最小/最大容量保证 | 应用在时间维度上平均获得资源 |
| 弹性 | 空闲资源可被其他队列借用 | 支持最小资源保证、优先级权重 |
| 抢占 | 支持 | 支持（preemption） |
| 适用场景 | 生产环境有明确 SLA | 多用户共享集群、短作业快速完成 |

两种调度器均支持 ACL 控制。

### 6. MapReduce 计算模型

MapReduce 分为 Map（映射）和 Reduce（归约）两个阶段：

![[assets/hadoop/diagram-mapreduce-flow.svg]]

- **Map 阶段**：将输入数据切分为独立的分片，并行处理生成中间键值对
- **Reduce 阶段**：将相同键的所有中间值合并处理输出最终结果
- MapReduce 在 hadoop-2.x 以上运行在 YARN 之上，保持与 hadoop-1.x 的 API 兼容

> 关键点：MapReduce 遵循数据本地化（Data Locality）原则——计算任务尽量调度到数据所在的节点；适合批处理作业，不适合实时/交互式查询。

### 7. HDFS 高可用（HA）架构

使用 Quorum Journal Manager (QJM) 实现 Active/Standby NameNode 架构：

- 至少 3 台 JournalNode 构成奇数个仲裁组
- Active NameNode 将 EditLog 写入多数 JN
- Standby NameNode 持续从 JN 读取并应用 EditLog 保持状态同步
- DataNode 同时向所有 NameNode 发送心跳和块报告
- 自动故障转移依赖 ZooKeeper 实现

> 关键点：QJM 是推荐方案（替代 NFS）；必须保证同时只有一个 Active NameNode 防止脑裂；Standby NameNode 自动承担 checkpoint 任务。

## 常见问题排查

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| NameNode 卡在 Safemode 无法退出 | 未收到 99.9% 数据块的心跳/块报告 | `hdfs dfsadmin -safemode leave`（谨慎）；调整 `dfs.namenode.safemode.threshold-pct`；`hdfs dfsadmin -report` 检查块分布 |
| DataNode 被标记 dead / Stale | 心跳超时（网络分区、宕机、GC 停顿） | `ping` / `telnet <dn> 9866` 检查连通；排查 DN 日志 OOM/GC；配置 `dfs.namenode.stale.datanode.interval`；检查磁盘空间 |
| 写 HDFS 报 Disk Out of Space | 部分 DataNode 磁盘不均衡用满 | `hdfs dfsadmin -report` 识别不均衡节点；`hdfs balancer` 重平衡；配置 `dfs.datanode.du.reserved`；`hdfs dfs -expunge` 清理 Trash |
| YARN 应用一直 ACCEPTED 不运行 | Scheduler 无法分配容器（资源满/超队列上限/配置不匹配） | `yarn application -list`；`yarn top`；检查 `yarn.scheduler.minimum/maximum-allocation-mb`；`yarn queue -status <queue>` |
| Reduce 阶段极慢（数据倾斜） | 某个键的中间数据量远大于其他键 | 自定义 Partitioner；启用 Combiner；对倾斜键加随机前缀（salt）；增加 Reducer 数量 |
| 空间不足但文件已删 — Trash 未及时回收 | `rm` 后文件先进 Trash，保留期内不释放 | `hdfs dfs -expunge` 强制清空；设置 `fs.trash.interval`（如 1440 分钟）；紧急用 `hdfs dfs -rm -skipTrash` |
| 机架感知未配置（副本放置非最优） | 未配置 topology 脚本，所有节点归 /default-rack | 配置 `topology.script.file.name`；重启 NameNode；`hdfs dfsadmin -printTopology` 验证 |

## 最佳实践

1. **合理配置 HDFS 块大小**：官方建议 128MB 或 256MB。大块减少 NameNode 内存占用（每块约 150 字节元数据），降低寻址开销，提高数据本地性；但 >512MB 会降低 Map 并行度。单文件小于块大小用默认 128MB，TB 级数据集用 256MB。
2. **使用 HDFS 透明加密保护敏感数据**：文件系统层级加密密钥由加密区域管理，数据块加密存储在 DataNode 磁盘，应用访问时自动解密。结合 KMS（Key Management Server）实现企业级加密。
3. **YARN 资源隔离配置**：设置 `yarn.nodemanager.resource.memory-mb` / `cpu-vcores` 为节点实际可用资源；启用 CGroups（LinuxContainerExecutor）实现 CPU/内存硬件级隔离，防止恶意应用影响其他容器。
4. **HA JournalNode 部署策略**：至少 3 个 JN（奇数个，容忍 1 台故障）；JN 轻量可与其他守护进程共置；使用独立磁盘保证 EditLog 写入 I/O；不建议在 HA 集群再运行 Secondary NameNode。
5. **定期执行数据均衡与磁盘均衡**：`hdfs balancer -threshold 10` 保持各 DataNode 使用率偏差 10% 以内；Hadoop 3.x+ 用 `hdfs diskbalancer -plan <datanode>` 平衡单节点多磁盘负载；建议每周 cron 执行。
6. **监控与告警体系**：核心指标 — NameNode Heap、HDFS 已用容量、DataNode 心跳、Missing Blocks、YARN 可用资源、队列等待数、Container 失败率。可用 Ambari/Cloudera Manager，或 Metrics → Graphite/InfluxDB + Grafana 轻量方案。

## 排查命令速查

```bash
# HDFS 状态与运维
hdfs dfsadmin -report                  # 查看 DataNode 状态、块分布、磁盘使用率
hdfs dfsadmin -safemode leave          # 手动退出安全模式（谨慎）
hdfs dfsadmin -printTopology           # 验证机架感知拓扑
hdfs balancer -threshold 10            # 数据重平衡（后台运行）
hdfs diskbalancer -plan <datanode>     # 单节点磁盘均衡（Hadoop 3.x+）
hdfs dfs -expunge                      # 清空 Trash 释放空间
hdfs dfs -rm -skipTrash <path>         # 跳过 Trash 直接删除

# YARN 作业与资源
yarn application -list                 # 查看所有应用状态
yarn top                               # 实时资源使用
yarn queue -status <queue>             # 查看队列容量与使用
```

## 相关笔记

- [[k8s-pod-deployment-service]] — Kubernetes 核心概念（同为分布式调度体系，可对照学习）
- [[docker-advanced-compose-swarm-security]] — 容器化与编排（Hadoop 常以容器方式部署）
- [[rocky-linux-ops-basics]] — Rocky Linux 运维基础（Hadoop 集群常见操作系统）
- [[k8s-cluster-ops-combat]] — 集群运维实战（排错思路可迁移）

## 官方参考

- [HDFS Architecture Guide](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HdfsDesign.html)
- [YARN Architecture Guide](https://hadoop.apache.org/docs/stable/hadoop-yarn/hadoop-yarn-site/YARN.html)
- [MapReduce Tutorial](https://hadoop.apache.org/docs/stable/hadoop-mapreduce-client/hadoop-mapreduce-client-core/MapReduceTutorial.html)
- [HDFS High Availability Using QJM](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HDFSHighAvailabilityWithQJM.html)
- [Capacity Scheduler Guide](https://hadoop.apache.org/docs/stable/hadoop-yarn/hadoop-yarn-site/CapacityScheduler.html)
- [Fair Scheduler Guide](https://hadoop.apache.org/docs/stable/hadoop-yarn/hadoop-yarn-site/FairScheduler.html)
- [HDFS Commands Guide](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HDFSCommands.html)
- [YARN Commands Guide](https://hadoop.apache.org/docs/stable/hadoop-yarn/hadoop-yarn-site/YarnCommands.html)
- [HDFS Erasure Coding Guide](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HDFSErasureCoding.html)
- [HDFS Transparent Encryption](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/TransparentEncryption.html)
- [Cluster Setup Guide](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-common/ClusterSetup.html)
- [HDFS Federation Guide](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/Federation.html)
- [Apache Hadoop GitHub 仓库](https://github.com/apache/hadoop)
