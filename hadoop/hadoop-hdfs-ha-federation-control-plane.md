---
created: 2026-09-17
tags: [hadoop, hdfs, high-availability, qjm, federation, viewfs, observability, 控制面]
topic: Hadoop生态
---

# HDFS 高可用（QJM 仲裁日志）与 NameNode 联邦控制面扩展

> 素材：Apache Hadoop 官方 trunk 分支 site markdown 原始文档（`HDFSHighAvailabilityWithQJM.md` / `HDFSHighAvailabilityWithNFS.md` / `Federation.md` / `ViewFs.md` / `ObserverNameNode.md` / `HdfsRollingUpgrade.md`）与 Apache JIRA 设计文档（HDFS-12943 / HDFS-13150 / HDFS-2185）。

## 概述

HDFS 把自身划分为两个层次：**Namespace**（目录 / 文件 / 块，承载全部命名空间操作）与 **Block Storage Service**（块管理 + 存储，由 DataNode 承担）。Hadoop 2.0.0 之前每集群只有一个 NameNode，它就是整个集群的单点故障（SPOF）：机器崩溃要等运维重启 NameNode，NameNode 机器的软硬件升级则造成计划内停机。

两个正交的控制面扩展解决这两类问题：

- **HDFS High Availability（HA）** —— 用同一集群内两套（Hadoop 3.0.0 起可多于两套）NameNode 组成 Active/Standby 热备，把非计划宕机与计划维护都变成秒级故障转移。状态同步的载体是**共享 edit log**：Quorum Journal Manager（QJM，一组 JournalNode 以多数派写入）或传统共享存储（NFS 挂载目录）。
- **HDFS Federation（联邦）** —— HA 解决「同一个命名空间不能倒」，Federation 解决「单个命名空间不够大」：把多个相互独立、互不协调的 NameNode/Namespace 联合起来，共用同一批 DataNode 的块存储，每个命名空间配一个独立 Block Pool，Namespace + Block Pool 合称 **Namespace Volume**（自包含的管理与升级单元）。

在此基础上还有两个消费侧能力：**ViewFs**（客户端挂载表，类比 Unix mount table）把多个命名空间卷拼成对应用透明的统一视图；**Observer NameNode** 让非 Active 节点也能服务「一致读」，用 state ID 与 `msync()` 保证同一个客户端读到自己刚写入的数据。

本文聚焦**控制面（元数据面）**：多数派写入与单写者语义、ZKFC 自动故障转移、fencing 隔离、联邦命名空间扩展、ViewFs 客户端视图、Observer 一致读，以及 HA 场景下升级/回滚/finalize 的边界条件。数据面（块副本、EC、存储策略）见 [[hadoop-mapreduce-execution-hdfs-storage]]。

## 架构图

![[assets/hadoop/diagram-hdfs-ha-control-plane-arch.svg]]

*图：HA + Federation 控制面全景 —— 客户端经 ViewFs 挂载表落到各自命名空间的 Active NameNode；Active 把 edit 写入 JournalNode 多数派，Standby/Observer 从 JN 尾部拉取并应用；ZooKeeper + ZKFC 负责健康监控、选举与隔离；DataNode 同时向全部 NameNode 心跳与块汇报，并承载所有 Block Pool。*

## 核心概念

### NameNode 单点故障（SPOF）与 HA 目标

Hadoop 2.0.0 之前每集群只有一个 NameNode，机器或进程不可用则整个集群不可用，直到运维重启 NameNode 或把它迁到另一台机器。这从两方面损害可用性：**非计划事件**（机器崩溃）期间集群停摆；**计划维护**（NameNode 机器上的软件/硬件升级）造成停机窗口。HA 通过在同一个集群内运行两个（3.0.0 起可多于两个）冗余 NameNode，以 Active/Passive 热点待命方式提供快速的非计划故障转移与运维发起的优雅故障转移。

> HA 消除的是「元数据面单点」；只有两套 NN 具备等价硬件（与非 HA 集群同规格）才算真正冗余。

### QJM 多数派写入与单写者语义

Quorum Journal Manager 用一组独立守护进程 **JournalNode** 承载共享 edit log：

- Active NameNode 每次命名空间修改都写往 JN 的**多数派**（majority）；
- Standby NameNode 读取 JN 上的 edit 并持续观察变化，随读随应用；
- 故障转移时 Standby 先确认自己已读完 JN 上全部 edit，再把自己提升为 Active，保证切换后命名空间完整同步；
- JN **只允许一个 NameNode 成为写入者**：切换时新 Active 接管写入角色，旧 Active 因无法再写 JN 而自动失效 —— 因此 QJM 本身已消除脑裂导致元数据损坏的可能。

> JN 数量必须至少 3 个且建议用奇数（3/5/7）：N 个 JN 最多容忍 `(N-1)/2` 个故障；偶数不增加容错还增加开销。

### JournalNode 部署与 `dfs.namenode.shared.edits.dir`

JournalNode 守护进程相对轻量，允许与 NameNode、JobTracker、YARN ResourceManager 等**共存于同一台机器**。共享 edits 的 URI 只配置一条，形如：

```
qjournal://host1:8485;host2:8485;host3:8485/journalId
```

默认端口 **8485**。Journal ID 唯一标识该 nameservice，使同一组 JN 能同时为多个联邦命名系统提供存储，建议直接复用 nameservice ID。JN 本地状态路径由 `dfs.journalnode.edits.dir` 指定，**只允许单一路径**，冗余靠部署多个 JN 或本地 RAID 保证。多个 JN 地址写在一条 value 里；另可用 `dfs.namenode.edits.qjournals.resolution-enabled=true` 配合 DNS round-robin 解析，避免硬编码主机名。

### nameservice ID 与 NameNode ID 两级配置后缀

HA 复用 Federation 的 **nameservice ID** 概念来标识一个可能含多台 HA NameNode 的 HDFS 实例，并新增 **NameNode ID** 区分具体节点。为让全集群共用同一份配置文件，相关参数以 nameservice ID（必要时再叠加 NameNode ID）作后缀：

```xml
<property>
  <name>dfs.nameservices</name>
  <value>mycluster</value>
</property>
<property>
  <name>dfs.ha.namenodes.mycluster</name>
  <value>nn1,nn2,nn3</value>
</property>
<property>
  <name>dfs.namenode.rpc-address.mycluster.nn1</name>
  <value>machine1.example.com:8020</value>
</property>
```

`dfs.nameservices` 与 `dfs.ha.namenodes.[ns]` 的取值决定后续所有配置项的键名，**必须先定这两项**。

> HA 最少 2 个 NameNode，建议 3 个、不超过 5 个，避免通信开销；同一份配置下发全部节点，无需按节点类型分发不同文件。

### 客户端故障转移代理（proxy provider）

客户端通过 `dfs.client.failover.proxy.provider.[nameserviceId]` 指定用于发现当前 Active 的 Java 类：

| 实现 | 行为 |
|------|------|
| `ConfiguredFailoverProxyProvider` | 按配置逐个尝试 NameNode |
| `RequestHedgingProxyProvider` | 首次调用并发问所有 NN 判定 active，之后只用 active 直到发生切换 |
| `ObserverReadProxyProvider` | 继承前者，读请求优先走 Observer，全部 Observer 失败才回退 Active |
| `ObserverReadProxyProviderWithIPFailover` | 上者的 IP 故障转移变体 |

读多写少的集群把客户端换成 `ObserverReadProxyProvider` 才能吃到 Observer 的负载分摊；不想改行为的客户端可继续用 `ConfiguredFailoverProxyProvider`。

### fencing（隔离）方法与 QJM 下的必要性

`dfs.ha.fencing.methods` 配置一组用回车分隔的隔离方法，故障转移时按序尝试直到有一个返回成功。官方自带三种：

| 方法 | 说明 |
|------|------|
| `sshfence` | SSH 到目标节点用 `fuser` 杀掉占用服务端口的进程；需配合 `dfs.ha.fencing.ssh.private-key-files` 免密登录，可写 `sshfence([[username][:port]])` 并配 `dfs.ha.fencing.ssh.connect-timeout` |
| `shell` | 执行任意命令，exit code 0 视为成功；可用 `$target_host`/`$target_port`/`$target_address`/`$target_nameserviceid`/`$target_namenodeid` 变量，**无内置超时**，必须自行在脚本里加超时 |
| `powershell` | 仅 Windows，按 CommandLine 唯一串匹配杀 `java.exe` 进程 |

**即便使用 QJM（已保证只有单写者可写 JN），仍需配置 fencing**：旧 Active 可能在被切换后继续给客户端提供陈旧读，直到它尝试写 JN 失败退出为止。完全不隔离也必须给该配置项填值，例如 `shell(/bin/true)`；建议把「保证返回成功」的方法放在列表**最后一位**，以免 fencing 机制失效时反过来降低可用性。

### ZKFC 与基于 ZooKeeper 的自动故障转移

在手动 HA（只靠 `haadmin` 命令）之上叠加 ZooKeeper 集群与 **ZKFailoverController（ZKFC）** 进程。每个跑 NameNode 的机器上都有一个 ZKFC，职责为：

1. **健康监控** —— 周期性给本地 NN 发健康检查命令，判断健康/失能；
2. **ZooKeeper 会话管理** —— 本地 NN 健康时保持 ZK 会话；若本地 NN 为 active 则额外持有一个 ephemeral 锁 znode；
3. **基于 ZooKeeper 的选举** —— 发现无人持锁则尝试抢锁，抢到即赢得选举，负责执行一次故障转移：必要时 fence 掉前任 active，再把本地 NN 转为 active。

ZooKeeper 提供两项能力：**故障检测**（机器崩溃 → 会话过期 → 锁节点自动删除 → 其他节点获知并触发故障转移）与 **Active 选举**（用独占机制选出一个 active）。失败检测到切换的耗时由 `ha.zookeeper.session-timeout.ms` 决定，默认 **5 秒**。配置只需 `hdfs-site.xml` 的 `dfs.ha.automatic-failover.enabled=true` 与 `core-site.xml` 的 `ha.zookeeper.quorum`，随后用 `hdfs zkfc -formatZK` 初始化 ZK 中的 `/hadoop-ha` 状态。

![[assets/hadoop/diagram-hdfs-zkfc-failover-flow.svg]]

*图：ZKFC 自动故障转移流程 —— 健康检查维持会话与锁，失能触发会话过期与锁释放，抢锁成功即执行 fencing 隔离前任 active，隔离成功才转 active；隔离方法全部失败则不切换。*

> 从手动故障转移切到自动故障转移**必须先关闭集群**（不支持在线切换）；开启后 `start-dfs.sh` 会自动在每个 NameNode 机器拉起 ZKFC 并选出 active。

### ZooKeeper 安全（`ha.zookeeper.auth` / `ha.zookeeper.acl`）

安全集群应保护 ZK 中存储的 HA 信息，防止恶意客户端篡改元数据或触发假故障转移。做法：

```bash
# 认证文件（值前面的 @ 表示读文件而非内联）
# core-site.xml:
#   ha.zookeeper.auth = @/etc/hadoop/conf/zk-auth.txt
#   ha.zookeeper.acl  = @/etc/hadoop/conf/zk-acls.txt
# zk-auth.txt 内容形如：
#   digest:hdfs-zkfcs:mypassword
# 用 DigestAuthenticationProvider 生成摘要
java -cp $HADOOP_HOME/share/hadoop/common/lib/zookeeper-*.jar \
  org.apache.zookeeper.server.auth.DigestAuthenticationProvider hdfs-zkfcs:mypassword
# zk-acls.txt 内容形如（rwcda 权限）：
#   digest:hdfs-zkfcs:<摘要>:rwcda
# 改完 ACL 必须重新格式化 ZK 才生效
hdfs zkfc -formatZK
# 校验
zkCli.sh -server zk1:2181 getAcl /hadoop-ha
```

认证信息也可存放在 CredentialProvider（Hadoop credential provider API）。

### In-Progress Edit Log Tailing 与 JN 内存 edit 缓存

默认情况下 Standby 只应用已经 finalize 的 edit 段落，因此存在应用延迟。启用 `dfs.ha.tail-edits.in-progress=true` 后会同时开启 JournalNode 上的**内存 edit 缓存**，Standby 可从缓存直接取 in-progress 段落，把事务应用延迟压到**毫秒级**；缓存未命中时仍能取到，但延迟显著变大。

缓存大小两种配法（设了 bytes 则 fraction 不生效）：

- `dfs.journalnode.edit-cache-size.bytes` —— 默认 1MB，单条 edit 约 200 字节，约 5000 条事务；
- `dfs.journalnode.edit-cache-size.fraction` —— 按 JVM 最大内存比例，建议小于 0.9。

监控 JN 指标 `RpcRequestCacheMissAmountNumMisses`（未被缓存满足的请求数）与 `RpcRequestCacheMissAmountAvgTxns`（本应多缓存多少条事务）来调大小。该特性主要与 Standby/Observer 读配合使用（HDFS-12943 / HDFS-13150）。

### Block Pool、Namespace Volume 与 ClusterID（联邦）

**Block Pool** 是属于单一命名空间的一组块。DataNode 同时保存集群内所有块池的数据，各块池独立管理 —— 因此命名空间生成新块 ID 无需与其他命名空间协调，单个 NameNode 故障也不妨碍 DataNode 继续服务其他 NameNode。Namespace 与其 Block Pool 合称 **Namespace Volume**，是自包含的管理单元：删除某个 NameNode/命名空间时其在 DataNode 上的块池一并删除，集群升级时也以 namespace volume 为单位升级。

**ClusterID** 标识集群内所有节点，格式化第一个 NameNode 时生成或指定，必须用它格式化其他 NameNode 才能纳入同一联邦集群。

![[assets/hadoop/diagram-hdfs-federation-namespace-volume.svg]]

*图：联邦命名空间卷与 ViewFs 挂载表 —— 每个 Namespace Volume 由一组 HA NameNode 与独立 Block Pool 组成，ViewFs 在客户端把 /user、/data、/projects 分别链接到不同命名空间，共享的 DataNode 集群同时承载全部 Block Pool。*

联邦的三大收益：**命名空间横向扩展**（小文件多的大部署受益）、**吞吐随 NameNode 增加而扩展**、**多租户隔离**（实验性应用不再拖慢生产）。

### ViewFs 客户端挂载表（link / linkFallback / linkMergeSlash）

ViewFs 像本地文件系统一样实现 Hadoop FileSystem 接口，但**只允许链接其他文件系统**，因此对 Hadoop 工具完全透明（shell 命令照常可用）。挂载点写在标准配置文件中：

```xml
<property>
  <name>fs.viewfs.mounttable.clusterX.link./data</name>
  <value>hdfs://nn1:8020/data</value>
</property>
<property>
  <name>fs.viewfs.mounttable.clusterX.link./project</name>
  <value>hdfs://nn2:8020/project</value>
</property>
<!-- 未在挂载表里配置的路径落到默认文件系统 -->
<property>
  <name>fs.viewfs.mounttable.clusterX.linkFallback</name>
  <value>hdfs://nn1:8020</value>
</property>
<!-- fs.defaultFS 设为 viewfs://clusterX -->
```

也可用 `linkMergeSlash` 把挂载表根目录与另一个文件系统根目录合并。

> 应用内应使用 `/foo/bar` 这类**相对路径**而非 `viewfs://clusterX/foo/bar` 或 `hdfs://<具体NN>/...`；挂载表在**作业提交时**读取（`core-site.xml` 的 XInclude 在提交时展开），改动挂载表后需重新提交作业。

### ViewFs 的 Nfly 多副本挂载点

Nfly 挂载点让一个逻辑文件在多个文件系统上同步多副本，适用于跨集群、跨区域、本地与云混合的冗余场景，设计目标是不超过 **1GB** 的相对小文件（性能受单核/单网卡限制，因为逻辑运行在单个客户端 JVM 中，如 FsShell 或 MapReduce 任务）。

```
fs.viewfs.mounttable.global.linkNfly../ads=<uri1>,<uri2>,<uri3>
```

属性名中的两个连续点来自空置的中间段；URI 可指向不同区域的不同存储（`hdfs://datacenter-east/ads`、`s3a://models-us-west/ads`）或同一文件系统下的不同目录。Nfly 是**客户端侧的同步多写**，不替代 HDFS 自身的块副本；只适合中小文件。

### Observer NameNode 与一致读（state ID / msync）

Observer NameNode 是与 Standby 类似、但额外能像 Active 一样**服务一致读**的第三种 NN 状态，用来分摊读请求、避免 Active 成为瓶颈。为保证单客户端**读己所写**（read-your-own-writes），RPC 头里引入由事务 ID 实现的 **state ID**：客户端经 Active 写入后更新自己的 state ID，后续读把它带给 Observer，Observer 须先确保自身事务 ID 追上该 state ID 才返回结果。

跨客户端的**带外通信**（客户端 foo 通过非 HDFS 通道告诉 bar 某写入已完成）会导致 bar 读不到，为此新增轻量 `msync()`（metadata sync）命令：对 Active 更新 state ID，此后读即一致。客户端启动时会自动调用一次 `msync()`。

**NameNode 状态机**：active ↔ standby、standby ↔ observer 可互相转换，但 **active 与 observer 之间不能直接转换**；Observer 不参与 failover 选举（官方尚未实现）。

### HA 升级、Finalize 与 Rollback（QJM 分布式元数据）

HA 环境中升级更复杂，因为 NameNode 依赖的磁盘元数据天然分布：既在两台 HA NameNode 本地，也在承载共享 edits 的 JournalNode 上。

1. 关闭所有 NN 并安装新软件；
2. **启动全部 JN**（升级、回滚、finalize 期间任一 JN 不在线操作即失败 —— 官方用 critical 强调）；
3. 用 `-upgrade` 标志启动其中一台 NN（它不会像平常那样进 standby，而是直接转 active，同时升级本地存储目录与共享 edit log）；
4. 另一台 NN 此时已不同步，必须用 `-bootstrapStandby` 重新引导（**用 `-upgrade` 启动第二台是错误**）。

```bash
hdfs dfsadmin -upgrade query        # 查询升级状态
hdfs dfsadmin -finalizeUpgrade      # finalize（由当时 active 的 NN 完成共享日志 finalize，另一台删除其旧 FS 状态）
```

回滚需先关闭两台 NN，在**发起升级的那台**上执行回滚（本地目录与共享日志一并回滚），再启动它并对另一台执行 `-bootstrapStandby` 重新同步。

### HDFS Rolling Upgrade / Downgrade / Rollback 的边界

滚动升级允许逐个升级 HDFS 守护进程（DataNode 可独立于 NameNode 升级，一台 NameNode 可独立于其他 NameNode 升级，NameNode 可独立于 DataNode 与 JournalNode 升级），自 Hadoop 2.4.0 起支持。无停机升级的前提是集群已启用 **HA** 与**线兼容（wire compatibility）**；HA 场景只滚动 NameNode 与 DataNode，**滚动 JN 与 ZK 可能带来停机**。

```bash
# 准备
hdfs dfsadmin -rollingUpgrade prepare      # 生成回滚用 fsimage
hdfs dfsadmin -rollingUpgrade query        # 轮询到提示可继续
# 升级 NN：先 standby → failover → 再处理原 active，均以 -rollingUpgrade started 启动
# 升级 DN：分批 shutdownDatanode ... upgrade，getDatanodeInfo 确认后升级重启
hdfs dfsadmin -rollingUpgrade finalize
```

| 手段 | 语义 | 是否可滚动 | 约束 |
|------|------|-----------|------|
| Downgrade | 回退软件，**保留**用户数据 | 可滚动 | 仅当新旧版本的 NN 与 DN **布局版本都未变**时可行 |
| Rollback | 回退软件并**回退用户数据**（升级期间新建文件变不可用、删除文件被恢复） | **必须停机** | 总是支持 |

Downgrade **必须先降 DataNode 再降 NameNode**，因为协议只保证向后兼容不保证向前兼容（老 DN 能对 new NN 说话，反之不行）。downgrade 与 rollback 只能在滚动升级**已开始且尚未被终止**（finalize/downgrade/rollback 任一即终止）之间进行；finalize 之后无法回滚。联邦集群对每个命名空间分别执行 prepare/finalize，对每对 active/standby 执行 NN 升级。

### NFS 共享存储 HA（QJM 的替代方案）

除 QJM 外，HA 也可用共享 NFS 目录承载 edit log：Active 把命名空间修改写入共享目录中的 edit 文件，Standby 持续观察该目录并应用 edits，切换前 Standby 确保已读完共享存储上的全部 edits。

该方案目前**只支持单一共享 edits 目录**，因此系统可用性受该目录可用性限制 —— 要消除全部单点故障，需为该目录准备多条网络路径与存储自身（磁盘/网络/电源）的冗余，官方因此建议用高质量专用 NAS 设备而非普通 Linux 服务器。

> 关键差异在**防脑裂机制**：NFS 版依赖管理员配置的 fencing 来切断旧 Active 对共享存储的访问，而 QJM 版由 JournalNode 内在保证同一时刻只有一个写入者。

## 常见问题表

| # | 现象 | 根因 | 处理 |
|---|------|------|------|
| 1 | 出现两个 active NameNode，或 Active 被报告为 standby，命名空间有发散风险 | 手动模式下直接用 `transitionToActive` 且未隔离，或 fencing 缺失/无效；自动模式下 ZKFC 未全部运行 | `hdfs haadmin -getAllServiceState` / `-getServiceState` 确认状态；用 `hdfs haadmin -failover <from> <to>` 切换（会先尝试优雅降级，否则按 fencing 列表隔离）；配好 sshfence 或 shell 并在末尾放必定成功的方法 |
| 2 | Active 写入失败/卡住，日志报无法写 JournalNode，NN 停在安全模式 | QJM 要求每次 edit 写入达到多数派；存活 JN 不超过半数（3 个挂 2 个）时无法构成多数派 | 确认 JN 进程与 `dfs.journalnode.edits.dir` 磁盘状态，恢复至少 `(N+1)/2` 个 JN（3 个 JN 必须 ≥2 存活）；启动集群时先 `hdfs --daemon start journalnode`；JN 数据目录损坏时评估 `-bootstrapStandby` 或从另一台 NN 重新同步 |
| 3 | 配了自动故障转移，kill 掉 Active 后没有自动切换 | ZKFC 未启动/缺失；`ha.zookeeper.quorum` 配错导致 ZK 不可达；未执行 `hdfs zkfc -formatZK`；或试图在线从手动切到自动（官方不支持） | 每台 NN 确认 zkfc 进程（`hdfs --daemon start zkfc` 或 `start-dfs.sh`）；核对 `ha.zookeeper.quorum` 与 `dfs.ha.automatic-failover.enabled=true`；执行 `hdfs zkfc -formatZK`；切换模式前先停集群；验证方式为 `kill -9` active 的 JVM，另一台应在数秒内（`ha.zookeeper.session-timeout.ms` 默认 5 秒）变 active |
| 4 | 故障转移后客户端仍从旧 Active 读到过期数据 | QJM 下单写者保证元数据不损坏，但旧 Active 退出前仍可能响应**读**请求并返回陈旧视图；NFS 方案下无 fencing 时旧 Active 甚至可能继续写共享 edits | 配置至少一种真正生效的 fencing（推荐 sshfence 并配免密私钥）；必要用 shell 脚本自定义隔离；把保证成功的方法放列表最后；即便不隔离也必须填 `shell(/bin/true)` |
| 5 | ZooKeeper 整体宕机，担心 HDFS 不可用或无法启动 | 故障检测与选举依赖 ZK；但 ZK **不在** HDFS 数据/元数据读写路径上 | 官方 FAQ：ZK 崩溃期间不会自动故障转移，HDFS 继续运行不受影响，ZK 恢复后自动重连。把 ZK 部署为 3 或 5 节点、与 HDFS 元数据分盘，并对每台 NN 上的 ZKFC 与每个 ZK 节点做存活监控（某些 ZK 故障下 ZKFC 会意外退出） |
| 6 | HA 集群还跑着 Secondary NameNode / CheckpointNode / BackupNode，出现 checkpoint 异常 | Standby NameNode 本身承担命名空间 checkpoint，再叠加是冗余且错误的做法 | HA 集群中**不要**运行 Secondary NameNode、CheckpointNode、BackupNode（官方原文指出这么做是 error）；非 HA 改 HA 时原 Secondary 机器正好回收为 Standby |
| 7 | 执行 HA 升级/回滚/finalize 报错 | 元数据分布在两台 NN 本地与 JN 上，任一台 JN 未运行即失败；对第二台 NN 误用 `-upgrade` | 操作前确保**全部 JN 已启动**；`-upgrade` 只用于第一台 NN，第二台必须 `-bootstrapStandby`；`-upgrade query` 查状态，`-finalizeUpgrade` 终结；回滚需先同时关闭两台 NN |
| 8 | 非 HA 改造成 HA 后第二台 NN 启动失败或共享 edits 不足 | 两台 NN 本地元数据未同步，或 JN 上还没有足够 edit 事务 | 全新集群先 `hdfs namenode -format`；已格式化的在未格式化那台执行 `hdfs namenode -bootstrapStandby`（同时确保 JN 有足够 edits）；非 HA NN 直接改造则执行 `hdfs namenode -initializeSharedEdits`。HA NameNode 启动时初始状态一律是 **Standby** |
| 9 | 联邦新增 NameNode 后 DataNode 不向它汇报，新命名空间看不到数据 | DataNode 只认识启动时配置中列出的 NameNode，新增后需显式刷新 | 分发含新 NameNode 的配置并重启新 NN 与 Secondary/Backup，然后对**所有** DataNode 执行 `hdfs dfsadmin -refreshNamenodes <host>:<ipc_port>` |
| 10 | 联邦下 `rename` 文件/目录失败（如 `rename /user/joe/myStuff /data/foo/bar`） | `/user` 与 `/data` 属于不同命名空间，HDFS 从不允许跨 NameNode / 跨集群 rename | 跨命名空间搬迁改用 `distcp`（ViewFs 下可写 `distcp viewfs://clusterY/pathSrc viewfs://clusterZ/pathDest`）；设计阶段把需一起 rename 的路径规划到同一命名空间；调整存储分布时改挂载表即可，应用无感 |
| 11 | 联邦新增/淘汰 NameNode 后 Balancer 跑完数据仍不均衡 | Balancer 默认策略 `datanode` 只按 DataNode 维度平衡存储，且从设计上**不平衡命名空间** | 用 `hdfs --daemon start balancer -policy blockpool` 在块池维度同时也在 DataNode 维度平衡；命名空间层面的均衡需人工规划（调整挂载点分担负载） |
| 12 | 联邦中格式化新增 NameNode 后它没有加入集群 | 格式化时使用了不同的 ClusterID | 第一个 NN 用 `hdfs namenode -format [-clusterId <id>]` 并记录 ClusterID；其余 NN 必须 `hdfs namenode -format -clusterId <cluster_id>`；老版本升级启用联邦用 `hdfs --daemon start namenode -upgrade -clusterId <cluster_ID>` |
| 13 | 启用 Observer 一致读后，某些客户端读到比预期更旧的数据 | 跨客户端带外通信场景中 bar 的读可能看不到 foo 的写入；或客户端从未对 Active 刷新 state ID | 读取前调用 `msync()`；客户端启动时自动 msync 一次；为 `ObserverReadProxyProvider` 配 `dfs.client.failover.observer.auto-msync-period.<ns>`（0=每次读前 sync、正值=超过该时长未联系 Active 才 sync、负值=默认不自动 sync），注意每次刷新都有一次 Active RPC 开销 |
| 14 | 开启自动故障转移的集群里 `haadmin -transitionToObserver` 报异常 | Observer 未与自动故障转移完全集成，不参与选举；ZKFC 运行时会因缺少相应处理而失败 | 加 `-forcemanual`（`hdfs haadmin -transitionToObserver -forcemanual`）；或在 Observer 节点不启用 ZKFC；`transitionToObserver` 只能在 Standby 上执行，对 Active 执行会抛异常；Observer 只用于提供读 |
| 15 | ViewFs 挂载表改了但作业行为没变，或应用报找不到路径 | 挂载表在作业**提交时**读取，运行中的作业不感知变更；未配置的路径需要 linkFallback | 修改后重新提交作业；为未显式配置的命名空间设置 `linkFallback`；应用统一用 `/foo/bar` 相对路径；挂载表集中管理（`core-site.xml` XInclude 独立文件或 View File System Overload Scheme） |
| 16 | ViewFs 挂载点成千上万时客户端启动慢、配置臃肿 | 传统 KV 挂载表需客户端理解全部 KV，某些 FileSystem API 会对所有挂载点做操作（一条 `hadoop fs -put` 会触发对每个挂载点 `setVerifyChecksum` 并初始化其文件系统） | 改用**正则规则挂载点**（Regex Pattern Based Mount Points）用规则抽象取代逐条配置；结合 Overload Scheme 做集中挂载表管理 |
| 17 | 滚动升级想回退，不确定用 downgrade 还是 rollback，或顺序搞错 | 两者语义不同：downgrade 只回退软件、保留数据且可滚动；rollback 连用户数据一起回退且必须停机；协议只保证向后兼容 | 只回退软件用 downgrade（**先 DN 后 NN**）；要连数据状态一起回到升级前才用 rollback（关闭全部 NN/DN，恢复旧版本，`-rollingUpgrade rollback` 启动 NN1，对 NN2 `-bootstrapStandby`，DN 以 `-rollback` 启动）。两者只能在滚动升级开始后、被 finalize/downgrade/rollback 终止前进行 |

## 最佳实践

### JournalNode 用奇数且不少于 3 个，并规划好隔离方法

至少部署 3 个 JN（容忍 1 台故障），要真正提升容错就按 3/5/7 奇数扩展（N 个 JN 容忍 `(N-1)/2` 故障）；JN 轻量可与其他守护进程混部。同时必须为 `dfs.ha.fencing.methods` 配置至少一种有效隔离，建议把保证返回成功的方法放在列表末位；即使不做隔离也要填 `shell(/bin/true)` 而不能留空。`dfs.journalnode.edits.dir` 仅支持单一路径，冗余靠多 JN 或本地 RAID。

### HA 集群不要部署 Secondary NameNode / CheckpointNode / BackupNode

Standby NameNode 自身承担命名空间 checkpoint，额外部署这类节点官方明确指出是错误做法；非 HA 改 HA 时可直接把原 Secondary NameNode 的机器回收为 Standby，节省硬件。

### 启用自动故障转移前先停集群，并单独监控 ZKFC 与 ZooKeeper 存活

手动模式无法在线切换到自动模式，必须先关闭集群再改配置并执行 `hdfs zkfc -formatZK`。上线后对每台 NameNode 机器监控 ZKFC 进程（某些 ZK 故障下它会意外退出，退出即失去自动切换能力），并监控 ZK 仲裁中的每个节点。ZK 建议 3 或 5 节点、数据与 HDFS 元数据分盘，允许与 NameNode/RM 混部。

### 用 `/isActive` HTTP 端点作为负载均衡器健康探针

把 NameNode 置于 Azure/AWS 等负载均衡之后时，可用 `http://NN_HOSTNAME/isActive` 作为健康检查：该 NN 处于 Active 时返回 **200**，否则返回 **405**，使 LB 始终只把流量指向当前的 Active NameNode。

### 开启 in-progress edit tailing 与 JN 内存缓存，并监控缓存未命中指标

设置 `dfs.ha.tail-edits.in-progress=true` 同时启用 JN 内存 edit 缓存，可把事务在 Standby/Observer 上的可见延迟压到毫秒级；缓存按 `dfs.journalnode.edit-cache-size.bytes`（默认 1MB ≈ 5000 条事务）或 `dfs.journalnode.edit-cache-size.fraction`（建议 <0.9）配置。持续观察 `RpcRequestCacheMissAmountNumMisses` 与 `RpcRequestCacheMissAmountAvgTxns` 决定是否调大。

### 读密集型集群为客户端选用 ObserverReadProxyProvider 与 auto-msync

把读流量分给 Observer 能显著缓解 Active 瓶颈（读请求在典型环境中占多数）。客户端把 `dfs.client.failover.proxy.provider.<ns>` 设为 `ObserverReadProxyProvider`，读优先走 Observer、全失败才回退 Active；跨客户端一致性场景可用 `dfs.client.failover.observer.auto-msync-period` 做有界的自动刷新，但要清楚每次刷新都有一次 Active RPC 开销。不改代理的客户端保持原行为。

### Observer 至少按「一 active + 一 standby + 一 observer」起步

启用 Observer 需要多于 2 个 NameNode 的 HA 集群，最小可用组合是 3 个 NN 分别为 active/standby/observer；大型 HDFS 集群建议按读请求强度与 HA 要求部署两个或更多 Observer。状态机不允许 active 与 observer 直接互转。

### 联邦用统一 ClusterID 格式化，DataNode 变更后 refreshNamenodes，客户端统一走 ViewFs 相对路径

第一个 NameNode 格式化时确定 ClusterID 并让其余 NN 用同一 ID；新增 NameNode 后对全部 DataNode 执行 `hdfs dfsadmin -refreshNamenodes`。客户端尽量用 `/foo/bar` 相对路径（依托 `fs.defaultFS=viewfs://<mountTable>`）而不是硬编码具体 NameNode 地址；挂载表集中管理。跨命名空间搬迁用 distcp，不要指望 rename。

### 联邦下用 blockpool 策略做数据均衡，并单独规划命名空间均衡

Balancer 已支持多 NameNode，`-policy datanode` 为默认（DataNode 维度），`-policy blockpool` 同时按块池与 DataNode 维度平衡；但 Balancer 只平衡数据、从设计上不平衡命名空间，小文件过多的元数据压力需要靠挂载点规划或专门的命名空间治理解决。

### 无停机升级走 rollingUpgrade 三阶段并对联邦按命名空间逐对执行

准备（`rollingUpgrade prepare` → `rollingUpgrade query` 轮询）→ 升级 NN（先 standby 后 failover 再原 active，均以 `-rollingUpgrade started` 启动）→ 分批升级 DataNode（`shutdownDatanode ... upgrade` 后用 `getDatanodeInfo` 确认已停再升级重启，可按机架分批并行）→ `rollingUpgrade finalize`。联邦集群对每个命名空间分别 prepare 与 finalize、对每对 active/standby 分别升级。若新版本引入新特性，遵循「新特性先禁用 → 升级 → 再启用」的顺序。

### HA 升级/回滚/finalize 前先确认全部 JournalNode 在线

HA 升级涉及本地与共享 edit log 的双重变更，任一 JN 不在线都会让升级、回滚或 finalize 失败。标准顺序：关闭所有 NN 并安装新软件 → **启动全部 JN** → `-upgrade` 启动第一台 NN → 第二台 `-bootstrapStandby` 重新引导；回滚则先关闭两台 NN、在发起升级那台执行回滚、再对另一台 bootstrapStandby。

### 选 QJM 还是 NFS 共享存储：看是否有可靠的冗余共享存储

QJM 由 JournalNode 内在保证单一写入者，且 JN 多实例天然冗余，是默认推荐；NFS 方案只支持单一共享 edits 目录，可用性直接受该目录限制，官方要求为它准备多网络路径与磁盘/网络/电源层面的冗余，并建议使用高质量专用 NAS 而非普通 Linux 服务器。若已有成熟高可用 NAS 且希望复用，可选 NFS 方案，但必须显式配置 fencing 来切断旧 Active 对共享存储的访问。

### 安全集群要保护 ZooKeeper 中的 HA 数据

用 `ha.zookeeper.auth` 与 `ha.zookeeper.acl` 指向磁盘上的认证/ACL 文件（值前加 `@`，也可通过 CredentialProvider 提供），用 `DigestAuthenticationProvider` 生成摘要并在 zk-acls.txt 中以 `digest:<user>:<摘要>:rwcda` 形式配置，改完 ACL 后重跑 `zkfc -formatZK` 并在 ZK CLI 用 `getAcl /hadoop-ha` 校验，防止恶意客户端篡改 HA 元数据或触发假故障转移。

## 排查命令

```bash
# ---- HA 状态与手动切换 ----
hdfs haadmin -getAllServiceState              # 全部 NN 的 active/standby/observer 状态
hdfs haadmin -getServiceState nn1             # 单节点状态
hdfs haadmin -checkHealth nn1                 # 单节点健康检查
hdfs haadmin -failover nn1 nn2                # 优雅切换：先降级，失败则按 fencing 列表隔离
# 注意：transitionToActive / transitionToStandby 不做任何隔离，应极少使用

# ---- ZKFC 与 ZooKeeper ----
hdfs --daemon start zkfc                      # 启动 ZKFC（start-dfs.sh 会自动拉起）
hdfs zkfc -formatZK                           # 初始化/重置 ZK 中的 /hadoop-ha 状态
ps -ef | grep -E "[z]kfc|[z]ookeeper"         # 确认 ZKFC 与 ZK 进程存活
zkCli.sh -server zk1:2181 getAcl /hadoop-ha    # 校验 ZK ACL（安全集群）

# ---- JournalNode 与共享 edits ----
hdfs --daemon start journalnode                # 集群启动先起 JN（升级/回滚/finalize 必须全部在线）
# dfs.namenode.shared.edits.dir = qjournal://host1:8485;host2:8485;host3:8485/mycluster
# dfs.journalnode.edits.dir     = /data/dfs/journalnode   （仅支持单一路径）
# 监控指标：RpcRequestCacheMissAmountNumMisses / RpcRequestCacheMissAmountAvgTxes

# ---- NameNode 元数据同步（新集群/改造/单台故障重建）----
hdfs namenode -format                          # 全新集群第一台
hdfs namenode -bootstrapStandby                # 第二台 NN 复制元数据（同时确保 JN 有足够 edits）
hdfs namenode -initializeSharedEdits           # 非 HA NameNode 原地改造成 HA
hdfs namenode -format -clusterId <cluster_id>  # 联邦：其余 NN 必须使用同一 ClusterID

# ---- HA 升级 / 回滚 / finalize ----
hdfs dfsadmin -upgrade query                   # 查升级状态
hdfs dfsadmin -finalizeUpgrade                 # finalize（此后无法回滚）
# 升级：关闭全部 NN → 启动全部 JN → 第一台 NN 加 -upgrade 启动 → 第二台 -bootstrapStandby
# 回滚：关闭两台 NN → 在发起升级那台回滚 → 启动它 → 对另一台 -bootstrapStandby

# ---- 滚动升级（需 HA + wire compatibility，自 2.4.0）----
hdfs dfsadmin -rollingUpgrade prepare
hdfs dfsadmin -rollingUpgrade query
# NN 以 -rollingUpgrade started 启动，先 standby → failover → 原 active
hdfs dfsadmin -shutdownDatanode <host>:<port> upgrade
hdfs dfsadmin -getDatanodeInfo <host>:<port>
hdfs dfsadmin -rollingUpgrade finalize

# ---- 联邦运维 ----
# dfs.nameservices = ns1,ns2,ns3
# dfs.ha.namenodes.ns1 = nn1,nn2
hdfs dfsadmin -refreshNamenodes <datanode_host>:<datanode_ipc_port>   # DataNode 识别新增 NN
hdfs --daemon start balancer -policy blockpool                        # 块池维度均衡
# 客户端挂载表：fs.defaultFS = viewfs://clusterX
#   fs.viewfs.mounttable.clusterX.link./data = hdfs://nn1:8020/data
#   fs.viewfs.mounttable.clusterX.linkFallback = hdfs://nn1:8020

# ---- Observer ----
hdfs haadmin -transitionToObserver -forcemanual   # 自动故障转移集群下必须加 -forcemanual
# dfs.client.failover.proxy.provider.clusterX = org.apache.hadoop.hdfs.server.namenode.ha.ObserverReadProxyProvider
# dfs.client.failover.observer.auto-msync-period.clusterX = 0   # 0=每次读前 sync；负值=默认不自动 sync

# ---- 负载均衡器健康探针 ----
curl -s -o /dev/null -w "%{http_code}\n" http://nn-host:9870/isActive   # Active 返回 200，否则 405
```

## 相关笔记

- [[hadoop-ecosystem]] —— Hadoop 生态全景（含 HA 概览节），本篇是其控制面深潜
- [[hadoop-core-internals]] —— HDFS/YARN 核心机制（Safemode、块放置、纠删码、QJM HA 要点）
- [[hadoop-mapreduce-execution-hdfs-storage]] —— 数据面：MR 执行机制与 HDFS 存储体系（存储策略、加密、SPS/Mover）
- [[hadoop-yarn-scheduling-ecosystem]] —— YARN 调度与容错（含 YARN Federation，与 HDFS Federation 互补）
- [[hadoop-yarn-container-hetero-scheduling]] —— YARN 容器化运行时与异构资源调度

## 官方参考

- [HDFS High Availability Using the Quorum Journal Manager](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HDFSHighAvailabilityWithQJM.html) —— HA 主文档（架构 / JN 多数派 / 配置细节 / fencing / 自动故障转移 / HA 升级回滚）
- [HDFS High Availability Using the NFS](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HDFSHighAvailabilityWithNFS.html) —— 共享存储 HA 替代方案 / 硬件与 fencing 要求
- [HDFS Federation](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/Federation.html) —— 多 Namespace / Block Pool / ClusterID / 联邦配置与运维
- [ViewFs Guide](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/ViewFs.html) —— 挂载表 link/linkFallback/linkMergeSlash、Nfly 多副本、正则挂载点
- [Consistent Reads from HDFS Observer NameNode](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/ObserverNameNode.html) —— Observer 状态机 / state ID / msync / 部署与客户端配置
- [HDFS Rolling Upgrade](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HdfsRollingUpgrade.html) —— 无停机滚动升级 / 联邦升级顺序 / downgrade 与 rollback 边界
- [HDFS-12943](https://issues.apache.org/jira/browse/HDFS-12943) —— Consistent Reads from Observer NameNode 设计文档
- [HDFS-13150](https://issues.apache.org/jira/browse/HDFS-13150) —— Edit Tailing Fast-Path（Observer 低延迟 edit tailing）
- [HDFS-2185](https://issues.apache.org/jira/browse/HDFS-2185) —— HDFS Automatic Failover 设计文档（ZKFC 选举与隔离流程）
