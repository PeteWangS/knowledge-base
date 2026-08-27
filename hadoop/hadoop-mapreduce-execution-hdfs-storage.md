---
created: 2026-08-27
topic: Hadoop 生态
subtopic: MapReduce 执行机制深潜（作业生命周期/任务执行/调优）+ HDFS 存储体系（分层存储/内存/透明加密）+ YARN 资源模型与 NodeManager
tags: [hadoop, mapreduce, hdfs, yarn, nodemanager, storage, encryption, 官方文档]
---

# MapReduce 执行机制与 HDFS 存储体系深潜

> 本篇为 Hadoop 主题再次轮转的新角度笔记，取「计算执行机制」方向：MapReduce 作业从提交到完成的完整生命周期与任务执行细节（MapReduceTutorial 全文）、HDFS 存储体系（ArchivalStorage 分层存储 / MemoryStorage 内存存储 / TransparentEncryption 透明加密）、YARN 可扩展资源模型与 NodeManager 节点执行机制（健康检查、重启恢复、辅助服务）。与 [[hadoop-ecosystem]]（全景概述）、[[hadoop-core-internals]]（HDFS/YARN 核心机制）、[[hadoop-yarn-scheduling-ecosystem]]（YARN 调度容错与生态工具）互补。内容出自 Apache Hadoop 官方文档（MapReduceTutorial / ArchivalStorage / MemoryStorage / TransparentEncryption / NodeManager / ResourceModel）。

## 概述

MapReduce 是 Hadoop 生态的核心计算模型：**job 将输入数据集切分为独立 InputSplit，由 map 任务并行处理**；框架对 map 输出排序后按分区交给 reduce 任务归并；框架自动负责调度、监控与失败任务重执行。HDFS 侧通过**存储类型与策略**实现计算与存储解耦，通过**加密区 + KMS + EDEK** 实现端到端透明加密；YARN 资源模型从 CPU/内存可扩展至 GPU 与自定义资源。本轮要点：

- **MapReduce 执行机制**：作业数据流模型 (k1,v1)→map→(k2,v2)→combine→reduce→(k3,v3)、InputSplit 与 map 任务数关系、Reducer shuffle/sort/reduce 三阶段、Partitioner/Combiner、MRAppMaster 子 JVM 执行环境、OutputCommitter 临时目录 promote 机制、压缩与可切分性、DistributedCache、Skipping Bad Records
- **HDFS 存储体系**：DISK/SSD/ARCHIVE/RAM_DISK/NVDIMM 存储类型与七种存储策略、SPS 与 Mover 块迁移（互斥）、LAZY_PERSIST 内存写入、透明加密（加密区/KMS/EDEK/DEK）、密钥轮换与 reencryptZone
- **YARN 资源模型与 NodeManager**：resource-types.xml 可扩展资源、NM 健康检查（磁盘 + 外部脚本）、NM 重启恢复（state-store）、辅助服务隔离

## 架构图

![[assets/hadoop/diagram-mr-job-lifecycle.svg]]

*图：MapReduce 作业从提交到完成的完整生命周期（MRv2/YARN）*

**架构要点**：

- **提交阶段**：Job Client 校验输入/输出规格、计算 InputSplit、设置 DistributedCache、拷贝 jar 到文件系统，然后提交给 ResourceManager
- **AM 阶段**：RM 为作业启动 MRAppMaster 容器；AM 按 InputSplit 数向 RM 申请 map 容器，并协调整个作业生命周期（进度监控、失败重试、推测执行）
- **任务执行**：map 任务在独立子 JVM 中执行，输出经 combiner 本地聚合后按 Partitioner 分区、排序并 spill 到本地磁盘；reduce 容器通过 HTTP 从各节点 aux-service（ShuffleHandler）拉取属于自己分区的 map 输出，内存/磁盘多路 merge 排序后调用 reduce 函数
- **提交输出**：结果经 OutputCommitter 从 `_temporary/_taskid` 临时目录 promote 到最终输出目录；推测执行的两个同任务实例写同一路径也不会冲突

## 核心概念

### MapReduce 作业数据流模型

框架只操作 (key, value) 对：`(input) <k1,v1> -> map -> <k2,v2> -> combine -> <k2,v2> -> reduce -> <k3,v3> (output)`。key/value 类必须实现 **Writable** 接口以便序列化，key 还需实现 **WritableComparable** 以支持框架排序；输入输出类型可以不同。计算节点与存储节点同置（数据本地性调度），框架优先在数据所在节点调度任务。

### Mapper 与 InputSplit

InputFormat 负责三件事：校验输入规格、将输入文件切分为逻辑 **InputSplit**、提供 RecordReader 将字节视图转为记录视图。**框架为每个 InputSplit 生成一个 map 任务**。FileInputFormat 按字节数切分，FileSystem blocksize 是 split 上限，`mapreduce.input.fileinputformat.split.minsize` 可设下限。TextInputFormat 是默认 InputFormat，按行处理。

### Reducer 三阶段：shuffle/sort/reduce

- **shuffle**：通过 HTTP 拉取所有 map 输出中属于本 reducer 分区的部分
- **sort**：按键分组归并（与 shuffle 同时进行，边拉取边合并）
- **reduce**：对每个 `<key, values 列表>` 调用 reduce 函数

reduce 任务数由 `Job.setNumReduceTasks` 设置；**Partitioner 分区总数 = reduce 任务数**。可用 `setSortComparatorClass` 与 `setGroupingComparatorClass` 组合实现二次排序。

### Partitioner 与 Combiner

Partitioner 控制中间 key 进入哪个 reduce 分区，默认 **HashPartitioner** 按 key 哈希，用户可自定义。**Combiner 是 map 端的本地聚合器**（通常复用 Reducer 类），先按 key 排序后做局部合并，减少跨网络传输的数据量——WordCount 例子中 combiner 使 map 输出 `<World,2>` 而非两个 `<World,1>`，显著压缩 shuffle 数据。

### MRAppMaster 与任务执行环境

MRAppMaster 将每个 Mapper/Reducer 任务作为**子进程在独立 JVM** 中执行，子进程继承 AM 环境。`mapreduce.{map|reduce}.java.opts` 配置子 JVM 参数（-Xmx、-Djava.library.path 等），其中 `@taskid@` 占位符会被替换为任务 ID。任务 stdout/stderr/syslog 由 NodeManager 收集到 `${HADOOP_LOG_DIR}/userlogs`。

### OutputCommitter 提交机制

FileOutputCommitter（默认）负责 job setup（创建临时输出目录）、task commit、job cleanup。每个 task-attempt 输出写入 `${mapreduce.output.fileoutputformat.outputdir}/_temporary/_${taskid}`（即 `${mapreduce.task.output.dir}`），**任务成功后仅 promote 该目录到最终输出目录**，失败/被杀任务输出被丢弃。该机制使推测执行的两个同任务实例写同一路径也不会冲突；JobSetup/JobCleanup/TaskCleanup 任务优先级最高。

### 输入输出格式与压缩

TextInputFormat/TextOutputFormat 为默认。框架自动检测 `*.gz` 输入并用 CompressionCodec 解压，但 **gzip 文件不可切分**（整个文件作为一个 map 输入）。输出可压缩：`FileOutputFormat.setCompressOutput` + `setOutputCompressorClass`；SequenceFileOutputFormat 支持 RECORD/BLOCK 两种压缩类型（默认 RECORD）。中间输出与 job 输出可独立配置压缩。

### DistributedCache 分布式缓存

分发应用需要的只读文件、归档、jar 到工作节点，**每个 job 每节点只拷贝一次**；支持 `-files/-libjars/-archives` 命令行或 `Job.addCacheFile` API。归档（zip/tar/tgz）自动解压并创建链接；缓存文件带执行权限；框架跟踪文件 mtime，作业执行期间不应修改缓存文件。子 JVM 的当前工作目录始终在 java.library.path 与 LD_LIBRARY_PATH 中，缓存的原生库可直接 `System.loadLibrary` 加载。

### Skipping Bad Records 坏记录跳过

当 map 任务对特定输入**确定性崩溃**（如第三方库 bug 无源码可修）且重试多次仍失败时，可启用跳过模式：记录正在处理的记录范围，跳过坏记录及其周围小范围数据继续执行。默认关闭，用 `SkipBadRecords.setMapperMaxSkipRecords` / `setReducerMaxSkipGroups` / `setAttemptsToStartSkipping` 启用。适合可容忍少量数据丢失的统计类作业；依赖 MAP_PROCESSED_RECORDS 计数器定位范围。

### HDFS 存储类型与存储策略

datanode 存储模型从单一存储扩展为**多存储集合**，类型含 DISK（默认）、SSD、ARCHIVE（高密度低成本冷存储）、RAM_DISK、NVDIMM（3.4+）。存储策略决定块放置：

| 策略 | 放置规则 |
|------|---------|
| Hot | 全 DISK |
| Cold | 全 ARCHIVE |
| Warm | DISK + ARCHIVE 混合 |
| All_SSD / One_SSD | 全 SSD / 优先 SSD |
| Lazy_Persist | 先 RAM_DISK 后落 DISK |
| Provided / All_NVDIMM | 外部存储 / 全 NVDIMM |

**策略解析规则：显式指定 > 父目录继承 > 根目录默认策略**。`dfs.datanode.data.dir` 需用 `[TYPE]file:///path` 标注存储类型；`dfs.storage.policy.enabled` 默认 true，`dfs.storage.default.policy` 默认 HOT。

![[assets/hadoop/diagram-hdfs-storage-policy.svg]]

*图：HDFS 存储类型、策略解析与块迁移（SPS 与 Mover 互斥二选一）*

### SPS 与 Mover 块迁移

设置存储策略**只改元数据**，块的实际迁移由 **Storage Policy Satisfier**（SPS，NN 外部服务，周期扫描策略不匹配块并调度 datanode 移动）或 `hdfs mover` 工具执行。**两者互斥不能同时运行**：SPS 运行中启动 Mover 会失败，反之亦然。`hdfs storagepolicies -satisfyStoragePolicy -path` 触发；`dfs.storage.policy.satisfier.mode=external` 启动 SPS，`hdfs --daemon start sps` 拉起外部服务。

### LAZY_PERSIST 内存写入

单副本文件**先写入 RAM_DISK（tmpfs 挂载）再惰性持久化到 DISK**。需配置 `dfs.datanode.max.locked.memory`（与集中式缓存共用额度）并同步调大 DataNode 用户的 `ulimit -l`。tmpfs 大小受内核限制且内容在内存压力下可被 swap 到磁盘；HDFS 暂不支持 ramfs。**tmpfs 卷必须在 data.dir 标注 `[RAM_DISK]`**，否则 HDFS 视为非易失存储、节点重启数据丢失。

### HDFS 透明加密与 KMS

加密区（encryption zone）是特殊目录，写入透明加密、读取透明解密，**端到端加密**（客户端加解密，DataNode 只见密文字节流）。每文件有唯一数据加密密钥 DEK，**HDFS 只持久化加密后的 EDEK**。KMS 作为密钥库代理承担三职责：提供加密区密钥、生成 EDEK、解密 EDEK。支持嵌套加密区（最近祖先加密区的密钥生效）。`hdfs crypto -createZone -keyName <key> -path <path>`；路径必须是空目录；密钥名不支持大写。

![[assets/hadoop/diagram-hdfs-transparent-encryption.svg]]

*图：透明加密数据流（EDEK/DEK 加解密链路，加解密只在客户端）*

### 密钥轮换与 reencryptZone

`hadoop key roll` 生成密钥新版本后，**存量文件的 EDEK 仍用旧版本密钥加密**，需 `hdfs crypto -reencryptZone -start` 批量重加密（NameNode 操作，逐批持锁处理，可用 `dfs.namenode.reencrypt.batch.size` 与读写锁占比参数限流）。快照因不可变性质不参与重加密。`hdfs crypto -listReencryptionStatus` 跟踪进度。

### NodeManager 健康检查

NM 运行**磁盘检查**（2 分钟间隔：权限、只读、剩余空间；单盘 90% 利用率阈值；min-healthy-disks 默认 0.25 即 25% 磁盘通过才算健康）与**外部脚本检查**（默认 10 分钟间隔、20 分钟超时）。健康状态随心跳上报 RM，**不健康节点停止分配新容器**。脚本 exit code 非 0 不算失败（可能只是语法错误）；输出 `ERROR` 开头行、超时或无法执行才判失败。

### NM Restart 容器恢复

NodeManager 重启恢复：NM 处理容器管理请求时将必要状态写入本地 state-store（`yarn.nodemanager.recovery.dir`，默认 `$hadoop.tmp.dir/yarn-nm-recovery`），重启后加载状态恢复运行中容器。需 `yarn.nodemanager.recovery.enabled=true` + `recovery.supervised=true`（退出时不清容器，假定立即重启恢复）。**`yarn.nodemanager.address` 必须固定端口**（不能是 0 临时端口），否则重启前后 RPC 端口变化导致客户端失联。

### YARN 可扩展资源模型

YARN 默认跟踪 CPU 与内存，可通过 **resource-types.xml** 定义任意可计数资源（GPU、软件许可证等）：`yarn.resource-types` 列资源名、`yarn.resource-types.<res>.units` 设单位（p/n/u/m/k/M/G/T 及 Ki/Mi/Gi/Ti/Pi 二进制）、可设 minimum/maximum-allocation。NM 侧用 **node-resources.xml** 的 `yarn.nodemanager.resource-type.<res>` 声明节点资源量，单位不一致时 RM 自动换算。资源名以字母开头、可带 `命名空间/` 前缀；memory-mb/vcores 为内置保留名不可重复定义。

## 常见问题表

| 问题 | 原因 | 解决 | 官方参考 |
|------|------|------|---------|
| Map 任务缓慢、磁盘 IO 高，日志显示频繁 spill | `mapreduce.task.io.sort.mb` 序列化缓冲过小或 `mapreduce.map.sort.spill.percent` 阈值过低，map 输出频繁落盘；spill 进行中缓冲被写满时 map 线程还会阻塞等待 | 调大 io.sort.mb（注意与子 JVM -Xmx 平衡）；spill.percent 可提到 0.8-0.9（阈值只是触发点不是阻塞点）；开启 `mapreduce.map.output.compress=true` + snappy/lz4 降低 spill 磁盘 IO 与 shuffle 网络 IO | MapReduceTutorial「Task Execution & Environment - Map Parameters」「Data Compression」 |
| Reduce 阶段长时间卡在 shuffle，作业整体变慢 | map 中间输出未压缩导致 fetch 传输量大；`mapreduce.task.io.sort.factor` 太小导致磁盘段多轮合并，`mapreduce.reduce.merge.inmem.thresholds` 过小触发频繁内存 merge（实践中常设很高如 1000 或 0 禁用） | 压缩 map 中间输出（snappy/lz4）；调大 io.sort.factor 提高单轮合并段数；`mapreduce.reduce.shuffle.merge.percent` 调高（reduce 输入能全进内存时可到 1.0），并监控文件系统计数器观察 map 输出字节与进入 reduce 字节的差异定位瓶颈 | MapReduceTutorial「Shuffle/Reduce Parameters」 |
| Map/Reduce 容器被 NodeManager 杀死，任务反复失败 | `mapreduce.{map|reduce}.memory.mb` 小于子 JVM -Xmx 导致虚拟内存超限（官方要求 memory.mb 必须大于等于 -Xmx，否则 VM 可能无法启动）；或 -Xmx 过大超出 YARN 容器资源请求 | 确保 memory.mb >= 子 JVM -Xmx，并都落在 YARN 容器资源请求之内；用 java.opts 的 `-Xloggc:/tmp/@taskid@.gc` 或 JMX 观察子 JVM 内存；核对容器日志与 userlogs | MapReduceTutorial「Memory Management」 |
| gzip 压缩的输入文件只产生极少数 map 任务，或单 map 处理时间过长 | TextInputFormat 检测到 `*.gz` 会解压处理，但 gzip 压缩流不可切分，每个 gzip 文件整体只能作为一个 InputSplit | 换用可切分压缩格式：bzip2、带索引的 LZO/LZ4，或 SequenceFile + BLOCK 压缩；避免大文件打成单个 gz（可先按块拆分）；小 gz 文件可合并减少任务开销 | MapReduceTutorial「Job Input」 |
| 作业在 map 阶段确定性失败，重试多次仍失败且日志无有效线索 | map 函数对特定输入记录确定性崩溃（常见于第三方库 bug 且无源码可修），任务永远无法成功完成 | 统计类作业启用 Skipping Bad Records（setMapperMaxSkipRecords / setAttemptsToStartSkipping 跳过坏记录周围数据）；用调试脚本自动分析失败任务（`mapreduce.map.debug.script`，参数依次为 $stdout $stderr $syslog $jobconf，经 DistributedCache 分发）；或开 `mapreduce.task.profile` 采样 profiling（默认 0-2 号任务）定位 JVM 级问题 | MapReduceTutorial「Skipping Bad Records」「Debugging」「Profiling」 |
| 跨加密区移动/重命名文件报错（hdfs dfs -mv 失败） | HDFS 设计上限制跨加密区边界重命名：加密区→非加密区、非加密区→加密区、两个不同加密区之间均被禁止，防止密钥泄露后无法定位所有受影响文件 | rename 只允许在同一加密区内或双方都非加密；确需跨区移动时先复制内容到目标再删除源（走客户端解密-加密路径），或使用 distcp 复制到加密位置 | TransparentEncryption「Rename and Trash considerations」 |
| distcp 复制到加密目录时校验和（checksum）不匹配 | 复制到加密位置时目标文件使用新的 EDEK 重新加密，底层 block 数据与源必然不同，文件系统 checksum 不可能一致（默认 distcp 会做 checksum 比较） | 复制到加密位置加 `-skipcrccheck` 与 `-update` 跳过校验；超级用户跨集群备份/DR 时改用 `/.reserved/raw/` 虚拟路径前缀直读底层块数据 + `-px` 保留 EDEK 等扩展属性，字节级一致且免除解密-重加密开销（建议先在目标集群创建相同加密区） | TransparentEncryption「Distcp considerations」 |
| 设置了存储策略（如 COLD）但数据块并未移动到 ARCHIVE 存储 | 设置存储策略只修改元数据，块物理迁移需要 SPS 或 Mover 实际执行；且 SPS 与 Mover 互斥，若 SPS 已运行则 Mover 无法启动（反之亦然） | 执行 `hdfs storagepolicies -satisfyStoragePolicy -path <path>` 调度块移动（SPS 外部服务模式：`dfs.storage.policy.satisfier.mode=external` + `hdfs --daemon start sps`），或运行 `hdfs mover -p <path>`；切换前先确认另一方未运行；改模式可用 `hdfs dfsadmin -reconfig namenode` 热更新 | ArchivalStorage「Storage Policy Satisfier (SPS)」「Mover - A New Data Migration Tool」 |
| 启用 LAZY_PERSIST 后节点重启，内存中的数据丢失 | tmpfs 卷未在 `dfs.datanode.data.dir` 中标注 `[RAM_DISK]`，HDFS 将其当作非易失存储使用；或 `dfs.datanode.max.locked.memory` 未设置、DataNode 用户 `ulimit -l` 未同步调大 | data.dir 配置 `[RAM_DISK]/mnt/dn-tmpfs` 形式标注；设置 max.locked.memory（如 32GB）并同步调大 ulimit -l；`mount -t tmpfs -o size=32g` 挂载并写入 /etc/fstab 保证重启自动重建；LAZY_PERSIST 副本最终会惰性落盘 DISK，正常路径下数据不丢 | MemoryStorage「Setup RAM Disks on Data Nodes」「Use the LAZY_PERSIST Storage Policy」 |
| NodeManager 重启后运行中的容器全部丢失，或客户端连接 NM 失败 | `yarn.nodemanager.recovery.enabled` 默认 false，NM 退出时清理所有容器；即使开启恢复，`yarn.nodemanager.address` 使用临时端口（0，默认）会在重启前后使用不同端口 | yarn-site.xml 启用 `yarn.nodemanager.recovery.enabled=true` + 配置 recovery.dir + `yarn.nodemanager.recovery.supervised=true`；`yarn.nodemanager.address` 显式设为固定端口（如 0.0.0.0:45454）作为开启恢复的前置条件；辅助服务需各自支持恢复 | NodeManager「NodeManager Restart」 |
| 节点被标记为 unhealthy，但健康检查脚本看起来执行正常 | 健康脚本退出码非 0 不被视为失败（官方解释可能是脚本语法错误导致）；只有输出以 ERROR 开头的行、执行超时（默认 20 分钟）、或脚本无法执行（权限/路径错误）才判定不健康 | 脚本内用 `echo "ERROR ..."` 显式报告故障；检查脚本路径与执行权限；注意默认检查间隔 10 分钟、可配 `yarn.nodemanager.health-checker.interval-ms` / `timeout-ms`；最多支持 4 个脚本（`yarn.nodemanager.health-checker.scripts`） | NodeManager「Health Checker Service - External Health Script」 |

## 最佳实践

| 实践 | 说明 | 官方来源 |
|------|------|---------|
| Map 中间输出开启压缩 | `mapreduce.map.output.compress=true` + snappy/lz4 压缩中间输出，显著降低 shuffle 网络传输量与 spill 磁盘 IO；job 输出长期存储用 SequenceFileOutputFormat + BLOCK 压缩（优于 RECORD） | MapReduceTutorial「Data Compression」 |
| 利用数据本地性调度 | 计算节点与存储节点同置部署（MapReduce 与 HDFS 同节点），框架自动优先在数据所在节点调度 map 任务；配合副本放置与机架感知设计提升本地命中率 | MapReduceTutorial「Overview」 |
| 任务副作用文件写入 task.output.dir | 应用创建临时/副作用文件时写入 `${mapreduce.task.output.dir}`（_temporary/_taskid 子目录），任务成功后由 FileOutputCommitter 自动 promote，推测执行并发实例互不冲突，无需手工用 attempt-id 拼唯一文件名 | MapReduceTutorial「Task Side-Effect Files」 |
| 依赖分发统一走 DistributedCache | jar、原生库、只读数据文件用 -libjars/-files/-archives（或 addCacheFile）：每 job 每节点只拷贝一次，归档自动解压；子 JVM 的 cwd 自动进入 java.library.path/LD_LIBRARY_PATH，原生库可直接 System.loadLibrary；作业执行期间不要修改缓存文件 | MapReduceTutorial「DistributedCache」「Distributing Libraries」 |
| 冷热数据分层存储治理 | 热数据保持 HOT（全 DISK），归档数据设 COLD（全 ARCHIVE）或 WARM（DISK+ARCHIVE 混合），用 `hdfs storagepolicies -setStoragePolicy` 按目录设置、SPS/Mover 按策略迁移；ARCHIVE 节点用高密度低成本存储，存储容量与计算能力独立扩展 | ArchivalStorage「Storage Policies」「Storage Policy Satisfier」「Mover」 |
| 临时高频数据用 LAZY_PERSIST | 对写入后短时间读取、可容忍最终落盘的临时数据（如中间结果）用 LAZY_PERSIST 策略或 CreateFlag.LAZY_PERSIST：先写 RAM_DISK 提速、再惰性落盘；前提是正确标注 `[RAM_DISK]` 卷、配置 max.locked.memory 并同步 ulimit -l | MemoryStorage「Application Usage」 |
| 密钥轮换后立即 reencryptZone | 合规要求定期轮换加密密钥：`hadoop key roll <key>` 后必须 `hdfs crypto -reencryptZone -start -path <zone>` 将存量文件 EDEK 重加密到新密钥版本（快照除外），并用 -listReencryptionStatus 跟踪；NN 密集操作可调 `dfs.namenode.reencrypt.batch.size` 与读写锁占比限流参数 | TransparentEncryption「reencryptZone」 |
| 加密数据备份用 /.reserved/raw 直拷 | 超级用户跨集群备份/DR 加密数据时用 `/.reserved/raw/` 前缀直读底层块数据并加 `-px` 保留 EDEK 等扩展属性：字节级一致、免解密重加密开销；建议先在目标集群创建相同加密区避免意外，复制到加密位置时用 -skipcrccheck -update | TransparentEncryption「Distcp considerations」 |
| 生产集群启用 NM 重启恢复 | 开启 `yarn.nodemanager.recovery.enabled=true` + `recovery.supervised=true` + 固定 `yarn.nodemanager.address` 端口，NM 滚动升级或意外重启后运行中容器自动恢复，避免大规模任务重跑；辅助服务需各自支持恢复 | NodeManager「NodeManager Restart」 |
| 健康检查脚本用 ERROR 行而非退出码 | 自定义健康脚本以 `echo "ERROR ..."` 输出故障（官方明确 exit code 非 0 不算失败），配合磁盘健康检查（min-healthy-disks 默认 0.25、单盘 90% 阈值）与 10 分钟默认间隔，让 RM 自动停止向故障节点分配容器；最多 4 个脚本并行 | NodeManager「Health Checker Service」 |
| 自定义资源类型规范化建模 | 引入 GPU 等新资源时在 resource-types.xml 统一定义单位与 min/max allocation（命名规范：字母开头、可带 `命名空间/` 前缀），NM 用 node-resources.xml 声明节点量（单位不一致 RM 自动换算）；常用组合封装为资源 profiles（`yarn.resourcemanager.resource-profiles.enabled`）按需请求 | ResourceModel「Resource Manager」「Node Manager」「Resource Profiles」 |

## 排查命令

```bash
# ---- MapReduce 调优与排错 ----
# 序列化缓冲与 spill 阈值
mapreduce.task.io.sort.mb / mapreduce.map.sort.spill.percent
# map 中间输出压缩
mapreduce.map.output.compress=true
mapreduce.map.output.compress.codec=org.apache.hadoop.io.compress.SnappyCodec
# shuffle/reduce merge 调优
mapreduce.task.io.sort.factor / mapreduce.reduce.merge.inmem.thresholds
mapreduce.reduce.shuffle.merge.percent
# 内存约束（memory.mb 必须 >= 子 JVM -Xmx）
mapreduce.map.memory.mb / mapreduce.map.java.opts
# 子 JVM GC/JMX 观察
mapreduce.map.java.opts="-Xmx1024m -Xloggc:/tmp/@taskid@.gc"
# 任务日志位置（NM 收集）
${HADOOP_LOG_DIR}/userlogs
# 跳过坏记录（代码 API）
SkipBadRecords.setMapperMaxSkipRecords(conf, n); setAttemptsToStartSkipping(conf, n)
# 调试脚本（参数依次为 $stdout $stderr $syslog $jobconf）
mapreduce.map.debug.script
# 任务 profiling（默认 0-2 号任务）
mapreduce.task.profile=true

# ---- HDFS 存储策略与迁移 ----
hdfs storagepolicies -setStoragePolicy -path /data/cold -policy COLD
hdfs storagepolicies -getStoragePolicy -path /data/cold
hdfs storagepolicies -satisfyStoragePolicy -path /data/cold
# SPS 外部服务（与 Mover 互斥）
dfs.storage.policy.satisfier.mode=external
hdfs --daemon start sps
# Mover 迁移
hdfs mover -p /data/cold
# data.dir 存储类型标注（RAM_DISK 示例）
dfs.datanode.data.dir=[RAM_DISK]file:///mnt/dn-tmpfs,[DISK]file:///data1/dn
# LAZY_PERSIST 前提
dfs.datanode.max.locked.memory=32212254720   # 32GB，与 ulimit -l 同步

# ---- HDFS 透明加密 ----
# 创建加密区（路径必须是空目录，密钥名不支持大写）
hdfs crypto -createZone -keyName mykey -path /encrypted
hadoop key roll mykey
hdfs crypto -reencryptZone -start -path /encrypted
hdfs crypto -listReencryptionStatus -path /encrypted
# 加密数据直拷备份（字节级一致 + 保留 EDEK）
hdfs dfs -ls /.reserved/raw/encrypted
distcp -skipcrccheck -update -px /src /.reserved/raw/... # 跨集群 DR 场景

# ---- NodeManager 恢复与健康检查 ----
yarn.nodemanager.recovery.enabled=true
yarn.nodemanager.recovery.dir=/data/yarn-nm-recovery
yarn.nodemanager.recovery.supervised=true
yarn.nodemanager.address=0.0.0.0:45454          # 必须固定端口
yarn.nodemanager.health-checker.interval-ms / timeout-ms / scripts

# ---- YARN 可扩展资源模型 ----
# resource-types.xml（RM 侧）
yarn.resource-types=yarn.io/gpu
yarn.resource-types.yarn.io/gpu.units=G
# node-resources.xml（NM 侧，单位不一致 RM 自动换算）
yarn.nodemanager.resource-type.yarn.io/gpu=8
# 资源 profiles
yarn.resourcemanager.resource-profiles.enabled=true
```

## 相关笔记

- [[hadoop-ecosystem]] — Hadoop 生态全景概述（基础方法论篇）
- [[hadoop-core-internals]] — HDFS/YARN/MR 官方文档深度篇（核心机制）
- [[hadoop-yarn-scheduling-ecosystem]] — YARN 调度与容错深度 + 生态工具实战
