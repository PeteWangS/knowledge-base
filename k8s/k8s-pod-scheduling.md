---
created: 2026-08-18
tags: [k8s, kubernetes, scheduler, scheduling, pod, affinity, taint, toleration, topology, eviction, priority, design-principles]
---

# K8s 核心概念：Pod 调度与调度器设计原理

> **研究日期**：2026-08-17 | **归档日期**：2026-08-18 | **优先级**：🔴最高 | **知识分类**：Kubernetes 核心 · 调度设计

## 概述

Pod 调度是 Kubernetes 控制平面的核心设计之一：**kube-scheduler** 负责把每一个尚未绑定节点的 Pod 放置到最合适的节点上，完成「调度周期（选节点）→ 绑定周期（写回 API Server）」的闭环。

本轮研究聚焦调度侧的设计原理：

- **两阶段调度**：过滤 Filtering（剔除不可行节点）+ 打分 Scoring（对可行节点排序取最优）
- **插件化调度框架**（Scheduling Framework）：12 个扩展点组成的可插拔调度流程
- **节点选择机制**：nodeSelector / 节点亲和与反亲和 / Pod 间亲和与反亲和 / 污点与容忍 / 拓扑分布约束 / 优先级与抢占
- **requests/limits 如何参与调度决策**：requests 决定能否调度，limits 决定能跑多狠
- **节点侧兜底**：kubelet 节点压力驱逐（node-pressure eviction）

它与已覆盖的 [[k8s-core-objects-design]]（控制器模式：Deployment 如何创建并滚动更新 Pod）和 [[k8s-pod-deployment-service]]（Service 如何暴露 Pod）互补，共同构成 Pod 从声明、创建、放置、运行到被驱逐的完整设计链。素材全部来自 kubernetes/website 官方文档（2026-08-17 拉取）。

## 架构图

![[assets/k8s/diagram-k8s-scheduler-arch.svg]]

*图：kube-scheduler「观察-决策-绑定」闭环架构*

调度器整体是一个「观察-决策-绑定」闭环：

| 环节 | 说明 |
|------|------|
| 观察 | kube-scheduler 监听 API Server 中 `spec.nodeName` 为空的 Pod |
| 决策 | 调度周期（**串行执行**）：过滤 Filtering → 打分 Scoring → 取最高分节点（多个节点平分时随机选一） |
| 绑定 | 绑定周期（**可并发**）：通过 Binding 把决策写回 API Server |

关键设计：

- **可行节点集合为空** → Pod 保持 Pending，进入重试队列按退避重试，不会自动报错
- **控制面把节点条件（NodeCondition）映射为污点**（taint），调度器只检查污点而非节点条件做决策
- **双保险**：调度器抢占（preemption，控制面）与 kubelet 驱逐（eviction，节点侧）构成资源紧张时的双重兜底
- 过滤与打分行为由调度框架（Scheduling Framework）插件驱动，支持多调度配置档（profiles）与自定义插件

## 核心概念

### 1. kube-scheduler 两阶段调度：过滤与打分

调度器对每个未绑定 Pod 执行两步操作：

- **Filtering（过滤）**：找出可行节点（feasible nodes）——如 `PodFitsResources` 插件检查节点剩余资源是否满足 Pod 的 requests，不满足的节点被剔除
- **Scoring（打分）**：对可行节点按活跃打分规则排序，取最高分节点绑定；若多个节点同分则随机选择

影响决策的因素包括：资源需求、软硬件与策略约束、亲和/反亲和、数据本地性（data locality）、工作负载间干扰等。调度周期（过滤+打分）串行执行，绑定周期并发执行。

> 💡 **关键点**：调度只看「请求量」（requests）而非实时用量；可行节点为空时 Pod 保持 Pending 直到可调度，不会报错——这是 `0/N nodes available` 类 FailedScheduling 事件的根源。

### 2. Scheduling Framework 插件化扩展点

调度框架把调度流程拆成 12 个扩展点，插件按需注册，是「为什么 Pod 放这里」的可解释来源，也是自定义调度器的标准扩展点：

![[assets/k8s/diagram-scheduling-framework-flow.svg]]

*图：Scheduling Framework 扩展点执行流（含抢占分支）*

| 扩展点 | 语义 |
|--------|------|
| QueueSort | 队列排序，**全局唯一**插件 |
| PreFilter / Filter | 硬约束过滤；Filter 任一失败即短路跳过剩余插件 |
| PostFilter | 无可选节点时触发，**典型实现是抢占** |
| PreScore / Score / NormalizeScore | 打分与归一化 |
| Reserve / Unreserve | 有状态插件预留与回滚；**Unreserve 必须幂等** |
| Permit | 批准 / 拒绝 / 等待三种语义，**等待超时转为拒绝** |
| PreBind / Bind / PostBind | 绑定前检查 / 写回 / 绑定后通知 |

v1.18 起大多数插件默认启用，可配置多个 profile 适配不同负载。

> 💡 **关键点**：调度框架是「为什么 Pod 放这里」的可解释来源，也是自定义调度器的标准扩展点；抢占不是魔法，只是 PostFilter 的一个默认实现。

### 3. requests 与 limits 的资源语义

| 维度 | requests | limits |
|------|----------|--------|
| 用于 | 调度决策（节点上所有 Pod 的 requests 之和 ≤ 节点可分配量）与 CPU 权重分配 | 硬上限 |
| CPU | 权重分配依据 | 通过内核节流（throttling）强制执行 |
| 内存 | 调度容量账目 | **反应式**：超限不立即被杀，仅在内存压力时被 OOM killer 终止 |
| 只设 limits 时 | kubelet 推导 requests=limits | — |

单位规则：`1 CPU = 1 核`，`0.1 = 100m`，精度不小于 1m；内存 `M = megabyte`、`Mi = mebibyte`、**`m = millibyte`（400m 仅 0.4 字节）**。hugepages 不可超卖。

> 💡 **关键点**：requests 决定能否调度，limits 决定能跑多狠——两者语义不同，只设 limits 会被推导 requests=limits，白白浪费可调度容量。

### 4. nodeSelector 与节点亲和/反亲和

- **nodeSelector**：最简单的节点选择约束，要求节点具备**全部**指定标签
- **节点亲和**更富表达力：
  - `requiredDuringSchedulingIgnoredDuringExecution`：硬约束，等同 nodeSelector 但语法更强，不满足则 Pending
  - `preferredDuringSchedulingIgnoredDuringExecution`：软约束，weight 1-100 加权进打分，无匹配节点仍会调度
- 操作符：In / NotIn / Exists / DoesNotExist / Gt / Lt
- **IgnoredDuringExecution 语义**：节点标签后续变化**不影响已运行 Pod**（只在调度时评估）

> 💡 **关键点**：软约束（preferred）在无匹配节点时仍会调度，硬约束（required）不满足则 Pending——用软约束做首选、硬约束做底线。

### 5. Pod 间亲和与反亲和（inter-pod affinity）

基于**拓扑域内已有 Pod 的标签**约束新 Pod 的放置：

- `podAffinity` 吸引：与匹配 Pod 同域共存
- `podAntiAffinity` 排斥：与匹配 Pod 同域互斥
- 拓扑域由 `topologyKey`（如 `topology.kubernetes.io/zone`）界定
- `namespaceSelector` 支持跨命名空间匹配
- `matchLabelKeys`（beta）可与 `pod-template-hash` 配合：滚动更新期间亲和只匹配同 revision 的 Pod

⚠️ **计算开销大**：官方不建议在数百节点以上的大集群使用。

> 💡 **关键点**：required 反亲和配滚动更新时易卡死（新旧 revision 互相排斥占满拓扑域），`matchLabelKeys + pod-template-hash` 是官方解法。

### 6. 污点与容忍（Taints & Tolerations）

**污点**让节点排斥 Pod，**容忍**声明 Pod 可接受该污点：

| 效应 | 行为 |
|------|------|
| NoSchedule | 不调度新 Pod |
| PreferNoSchedule | 软排斥 |
| NoExecute | **驱逐已运行且无容忍的 Pod** |

匹配规则 Equal / Exists，另支持数值比较操作符 Gt/Lt（`TaintTolerationComparisonOperators` 特性门控）。控制面把节点条件映射为内置污点（not-ready、unreachable、memory-pressure、disk-pressure、pid-pressure 等），其中：

- not-ready / unreachable 默认 NoExecute 且**默认容忍 300 秒**（tolerationSeconds=300）
- DaemonSet 控制器自动添加常见压力污点的容忍

> 💡 **关键点**：调度器检查污点而非节点条件；`spec.nodeName` 手动指定可绕过调度器，但 NoExecute 驱逐仍会生效。

### 7. Pod 优先级与抢占（Priority & Preemption）

- **PriorityClass**（非命名空间对象）定义优先级整数值（-2³¹ 到 10⁹）；`system-cluster-critical`=2×10⁹、`system-node-critical`=2×10⁹+1000 保留给系统组件
- 高优先级 Pod 排队靠前；**无法调度时触发抢占**（PostFilter 默认实现）：选择能通过移除低优先级 Pod 使其可调度的节点，受害者获得优雅终止期（默认 30 秒）
- `preemptionPolicy: Never` 可声明非抢占优先级类
- `nominatedNodeName` 记录提名节点但**不保证**最终落点；PDB 保护是**尽力而为**；不做跨节点抢占；抢占不参考 QoS

> 💡 **关键点**：抢占是资源紧张时的最后手段，会真实杀死低优先级 Pod——优先级值要谨慎授予。

### 8. 拓扑分布约束（Topology Spread Constraints）

声明式控制 Pod 跨拓扑域（节点/可用区/区域）的均匀分布：

- `maxSkew`：允许的最大不均衡度（相对全局最小值）
- `minDomains`：最小合格域数
- `whenUnsatisfiable`：`DoNotSchedule`（不满足则保持 Pending）或 `ScheduleAnyway`（仅降权）
- 内置默认约束（v1.24 起稳定）：hostname maxSkew=3 + zone maxSkew=5，均为 ScheduleAnyway

已知限制：缩容后不保证仍均衡；不匹配自身 labelSelector 的 Pod 会产生「ghost pod」堆积；自动扩缩容到零节点的拓扑域不可见。

> 💡 **关键点**：同一 `topologyKey + whenUnsatisfiable` 只能有一个约束；节点缺 topologyKey 标签会被整体跳过——拓扑标签一致性是约束生效的前提。

### 9. 节点压力驱逐（Node-pressure Eviction）

kubelet 依据驱逐信号对比软/硬阈值决策：

![[assets/k8s/diagram-node-pressure-eviction.svg]]

*图：节点压力驱逐决策流程（软/硬阈值 + 驱逐顺序）*

| 维度 | 说明 |
|------|------|
| 驱逐信号 | memory.available、nodefs/imagefs/containerfs 的 available 与 inodesFree、pid.available |
| 硬阈值 | 无宽限立即杀（默认 memory.available<100Mi、nodefs<10%、imagefs<15%、inodesFree<5%） |
| 软阈值 | 需持续超过宽限期才驱逐 |
| 驱逐顺序 | 先驱逐 **usage 超过 requests** 的 BestEffort/Burstable Pod（按优先级、再用超量比例），Guaranteed 与 usage≤requests 的 Pod 最后驱逐 |
| OOM 兜底 | 按 `oom_score_adj` 杀容器（Guaranteed -997、BestEffort 1000） |

> 💡 **关键点**：驱逐按「用量是否超 requests + 优先级」排序而非 QoS 类；Guaranteed Pod 也非绝对安全（超限仍会被杀）。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|---------|---------|
| Pod 一直 Pending，事件显示 FailedScheduling（0/N nodes available） | 过滤阶段没有任何可行节点：节点资源不足（Insufficient cpu/memory）、节点不匹配 nodeSelector/亲和规则、节点存在未容忍的污点、反亲和或拓扑约束不满足。调度器把 Pod 放回队列按退避重试，不会自动报错 | `kubectl describe pod <name>` 查看 Events 中 FailedScheduling 的具体原因（如 `0/3 nodes available: 1 Insufficient memory, 2 node(s) didn't match node selector`）；对照 `kubectl get nodes` 的 allocatable 与标签、`kubectl describe node` 确认污点；修正 requests、标签、容忍或约束后 Pod 会在下轮重试中自动调度 | [kube-scheduler](https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/) |
| 节点故障/失联后运行中的 Pod 被自动驱逐 | 节点控制器对 not-ready（Ready=False）与 unreachable（Ready=Unknown）节点自动打上 NoExecute 污点，普通 Pod 的默认容忍仅 300 秒（tolerationSeconds=300），宽限期一到即被驱逐；节点长时间失联时 Pod 被标记删除 | 对关键工作负载在 PodSpec 显式声明更长 tolerationSeconds 的容忍（或容忍 while 存在即可）；区分主动维护（先 cordon 再 drain，受 PDB 保护）与意外故障；排查节点网络/kubelet 状态，必要时驱逐并重新调度到健康节点 | [taint-based-evictions](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/#taint-based-evictions) |
| 高优先级 Pod 触发抢占后迟迟无法调度 | 被抢占的受害 Pod 有默认 30 秒优雅终止期，期间资源未释放；等待期间若出现更高优先级 Pod，节点可能被后者占用，抢占者的 nominatedNodeName 被清空、需重新找节点——「抢了但没轮到」 | 观察被抢占 Pod 的事件与抢占者的 nominatedNodeName；将低优先级工作负载的 terminationGracePeriodSeconds 调小（甚至为 0）缩短释放窗口；确认抢占者优先级确实高于受害者；避免在滚动窗口期频繁创建超高优先级 Pod | [pod-priority-preemption#troubleshooting](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/#troubleshooting) |
| 设置 topologySpreadConstraints 后 Pod 卡在 Pending | 多个约束的交集为空（如 zone 约束只允许 zone B、节点约束只允许 node2，二者无交集）；或节点缺少约束中 topologyKey 对应的标签（含拼写错误如 zone-typo），这些节点被整体跳过、Pod 无合法落点 | 调大 maxSkew、把次要约束改为 `whenUnsatisfiable: ScheduleAnyway`；用 `kubectl get nodes --show-labels` 核对节点拓扑标签一致性；保证集群内所有节点都具备约束用到的 topologyKey 标签（官方建议使用 topology.kubernetes.io/zone、region 等注册标签键） | [topology-spread-constraints#example-conflicting](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/#example-conflicting-topologyspreadconstraints) |
| 节点反复出现 Evicted Pod，工作负载频繁被杀 | 节点内存/磁盘/索引节点压力触发 kubelet 驱逐阈值（默认硬阈值 memory.available<100Mi、nodefs<10%、imagefs<15%）；Pod 实际用量超过 requests 时按「超量 + 低优先级优先」顺序被驱逐，资源未被有效回收会反复触发 | 为容器设置合理的 requests（真实基线）与 limits，Guaranteed Pod 最后被驱逐；用 `--eviction-hard` 调整阈值、`--eviction-minimum-reclaim` 设定最小回收量防止抖动；用 `--system-reserved/--kube-reserved` 为系统守护进程预留内存，避免系统占用挤占 Pod 配额 | [node-pressure-eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/) |
| 内存单位写错：400m 被解析成 0.4 字节 | Kubernetes 内存 quantity 后缀区分大小写：M 表示 megabyte，**m 表示 millibyte（1/1000 字节）**。写 `memory: 400m` 实际请求 0.4 字节，Pod 可被调度但容器一启动就内存异常/OOM；Mi/Gi 等幂次后缀才是常用正确写法 | 内存一律使用 Mi/Gi/Ki 幂次后缀（如 `memory: 400Mi`）；CPU 用 m 后缀（如 `cpu: 250m`）或小数；提交前用 `kubectl apply --dry-run=client` 与 `kubectl describe` 复核解析后的 quantity 值 | [manage-resources-containers#meaning-of-memory](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#meaning-of-memory) |
| 滚动更新期间新副本因反亲和全部 Pending | required 型 podAntiAffinity 的 labelSelector 同时匹配旧、新 revision 的 Pod（labelSelector 不含 revision 区分），滚动更新新旧并存时把拓扑域占满，新副本无合法落点——Deployment 滚动卡死 | 在反亲和规则的 `matchLabelKeys` 中加入 `pod-template-hash`（Deployment 为每个 ReplicaSet 自动打该标签），使亲和只匹配同 revision 的 Pod；或把 required 改为 preferred 软约束；紧急恢复可临时提高 maxSurge 或删除卡住的旧 Pod | [assign-pod-node#matchlabelkeys](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/#matchlabelkeys) |
| 节点明明空闲（实时用量很低）却报 Insufficient memory 拒绝调度 | 调度决策基于「节点上所有 Pod 的 requests 总和 vs 节点可分配量（allocatable = capacity − kube-reserved − system-reserved − 驱逐阈值）」的静态容量检查，与实时用量无关——这是防止高峰超卖的设计行为，不是故障 | `kubectl describe node` 查看 Allocatable 与 Allocated resources 列确认容量账目；检查是否大量 Pod 设置了过高的 requests 或只设 limits（被推导为 requests=limits）；优先压缩 requests 而非盲目加节点 | [manage-resources-containers#how-pods-with-resource-requests-are-scheduled](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#how-pods-with-resource-requests-are-scheduled) |

## 最佳实践

### 1. 始终通过工作负载控制器管理 Pod，并显式设置优先级类

不要直接创建裸 Pod（无控制器不自动重建）；`system-cluster-critical`/`system-node-critical` 保留给真正的基础组件，业务优先级用自定义 PriorityClass 并按需配合 ResourceQuota 限制某优先级类的总消耗，防止误授予高优先级引发大面积抢占。

官方来源：[pod-priority-preemption](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/)

### 2. requests 表达真实用量基线，limits 作为硬上限

CPU limit 是硬节流、内存 limit 是反应式 OOM——两者语义不同；只设 limits 会被推导 requests=limits，既浪费调度容量又让 Pod 被误判为高占用；建议按监控基线设 requests，为突发留 limits 余量，并善用 VPA 做资源建议。

官方来源：[manage-resources-containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)

### 3. 节点隔离使用 node-restriction.kubernetes.io/ 前缀标签

用标签做节点隔离/合规分区时，选择 kubelet 无法修改的标签键：NodeRestriction 准入插件禁止 kubelet 设置带该前缀的标签，防止被攻陷节点自我贴标骗取敏感工作负载调度到自身。

官方来源：[assign-pod-node#node-isolation-restriction](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/#node-isolation-restriction)

### 4. 保证集群拓扑标签一致且使用注册标签键

反亲和与拓扑分布约束依赖节点标签界定拓扑域：所有节点都应具备 `topology.kubernetes.io/zone`、`topology.kubernetes.io/region`、`kubernetes.io/hostname` 等标准标签，缺标签或拼写不一致的节点会被静默跳过，导致分布行为与预期不符；云上集群由云厂商自动填充，自建集群需纳入节点初始化流程。

官方来源：[topology-spread-constraints#consistency](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/#consistency)

### 5. 驱逐阈值与系统预留联动设计，避免「一调度就驱逐」

官方示例：10Gi 节点想为系统预留 10% 并在 95% 用量时驱逐，应配置 `--eviction-hard=memory.available<500Mi` 与 `--system-reserved=memory=1.5Gi`（10% 总量 + 阈值量），使调度可容纳量与驱逐触发线自洽；调整任一驱逐阈值参数时其余参数不会继承默认值，需全部显式给出或用 `MergeDefaultEvictionSettings=true`。

官方来源：[node-pressure-eviction#schedulable-resources-and-eviction-policies](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/#schedulable-resources-and-eviction-policies)

### 6. 沙箱/虚拟化运行时用 RuntimeClass 声明 Pod overhead

Kata Containers/Firecracker 等运行时每 Pod 有固定开销（示例 kata-fc：memory 120Mi + cpu 250m），通过 RuntimeClass 的 overhead 字段声明后，调度、ResourceQuota 与 Pod cgroup 都会计入该开销，避免节点资源被静默超卖；用 `kube_pod_overhead_*` 指标观测。

官方来源：[pod-overhead](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-overhead/)

## 排查命令

```bash
# --- 调度定位 ---
kubectl describe pod <name>                 # Events 中的 FailedScheduling 具体原因
kubectl get pod -o wide                     # 查看 Pod 被调度到哪个节点
kubectl get nodes --show-labels             # 核对节点标签与拓扑键
kubectl describe node <node>                # Allocatable / Allocated resources / 污点
kubectl get priorityclasses                 # 查看优先级类与抢占配置
kubectl get pdb -A                          # 查看 PodDisruptionBudget 保护

# --- 污点与节点维护 ---
kubectl taint nodes <node> key=value:NoSchedule   # 手动打污点
kubectl taint nodes <node> key-                   # 移除污点
kubectl cordon <node> && kubectl drain <node> --ignore-daemonsets --delete-emptydir-data

# --- 调度器日志与指标 ---
kubectl logs -n kube-system kube-scheduler-<name>     # 调度器日志（含抢占/重试）
kubectl get --raw /metrics | grep -E "schedule_attempts_total|scheduling_algorithm_duration"

# --- 节点压力驱逐观察 ---
journalctl -u kubelet | grep -i evict                 # kubelet 驱逐事件
kubectl get pods -A --field-selector=status.phase=Failed | grep Evicted
kubectl describe node <node> | grep -A5 "Non-terminated Pods"   # 容量账目
```

## 相关笔记

- [[k8s-core-objects-design]] — 控制器模式与 Deployment/Service 设计原理（调度链的上游：Pod 如何被声明与创建）
- [[k8s-pod-deployment-service]] — Pod / Deployment / Service 基础概念版
- [[k8s-cluster-ops-deepdive]] — 集群运维深度（Pod 排错 / etcd 恢复 / 节点维护）——本篇调度与驱逐原理的运维落地
- [[k8s-cluster-troubleshooting]] — 集群排障实战

**官方参考全集**：kube-scheduler（[scheduling-eviction/kube-scheduler](https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/)）｜Scheduling Framework（[scheduling-framework](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)）｜Assigning Pods to Nodes（[assign-pod-node](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)）｜Taints and Tolerations（[taint-and-toleration](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/)）｜Topology Spread Constraints（[topology-spread-constraints](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)）｜Priority and Preemption（[pod-priority-preemption](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/)）｜Node-pressure Eviction（[node-pressure-eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/)）｜Scheduler Performance Tuning（[scheduler-perf-tuning](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduler-perf-tuning/)）｜Pod Overhead（[pod-overhead](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-overhead/)）｜API-initiated Eviction（[api-eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/)）｜Resource Management（[manage-resources-containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)）
