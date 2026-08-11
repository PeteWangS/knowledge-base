---
created: 2026-08-11
tags: [k8s, pod, deployment, service, kubernetes, design-principles, controller-pattern]
---

# K8s 核心对象设计原理：Pod / Deployment / Service 底层机制

> **研究日期**：2026-08-11 | **优先级**：🔴最高 | **知识分类**：Kubernetes 核心 · 设计原理

## 概述

Kubernetes 的核心设计哲学是「**声明式 API + 控制器模式**」：用户只声明期望状态（Desired State），控制器通过调和循环（Reconcile Loop）不断把实际状态收敛到期望状态。Pod、Deployment、Service 三大对象构成了这套哲学的最小完整闭环：

- **Deployment** 声明应用的期望副本数与更新策略，通过 ReplicaSet 间接管理 Pod
- **Pod** 是调度与运行的最小原子单元，拥有独立生命周期状态机
- **Service** 为不断变化的 Pod 集合提供稳定的虚拟 IP 访问入口

本篇聚焦**设计原理层**：Pod 生命周期状态机与 QoS 驱逐优先级、Deployment 滚动更新与比例伸缩的内置约束、Service 虚拟 IP 与 kube-proxy 转发链路的底层机制（iptables/ipvs/nftables 模式对比），与 [[k8s-pod-deployment-service]]（基础概念版）互补。

## 架构图

![[assets/k8s/diagram-k8s-core-design-arch.svg]]

Kubernetes 采用**控制面与数据面分离**的主从架构：

| 平面 | 组件 | 职责 |
|------|------|------|
| 控制面 | kube-apiserver | 唯一 API 入口，所有组件交互的枢纽 |
| 控制面 | etcd | 状态唯一真实来源（source of truth） |
| 控制面 | kube-scheduler | 调度决策 |
| 控制面 | kube-controller-manager | 运行 Deployment / ReplicaSet / EndpointSlice 等各类控制器 |
| 数据面 | kubelet | 每节点管理 Pod 生命周期 |
| 数据面 | kube-proxy | 每节点维护 Service 转发规则 |

核心运行机制是**控制器模式**：以 Deployment 为例，控制链为 **Deployment → ReplicaSet → Pod** 三级对象，通过 `ownerReferences` 建立级联关系，每级控制器独立调和。Service 则独立于这条控制链：控制面为其分配虚拟 IP，EndpointSlice 控制器维护端点集合，kube-proxy 将发往 VIP 的流量 DNAT 到后端 Pod。整个系统通过「期望状态 vs 实际状态」的差异驱动收敛，任何组件故障都不影响其他组件的声明与调和。

## 核心概念

### 1. 声明式 API 与控制器模式

用户通过 API 提交期望状态（如 `replicas: 3`），控制器循环监视 API 对象，比较期望状态与实际状态，执行创建/删除/更新操作使实际状态向期望状态收敛。这是 Kubernetes 所有工作负载对象的统一设计基础。

> 💡 **关键点**：控制器只负责收敛，不负责保证即时一致；Pod 的故障恢复依赖**控制器替换而非原地修复**——一个 Pod（按 UID 标识）永远不会被重新调度到其他节点，而是被一个近似的全新 Pod 替换。

### 2. Pod 生命周期状态机（phase + conditions）

`status.phase` 提供高层摘要：

| Phase | 含义 |
|-------|------|
| Pending | 已被集群接受但容器未就绪（含等待调度与拉镜像） |
| Running | 已绑定节点且至少一个容器在运行 |
| Succeeded | 所有容器成功退出且不再重启 |
| Failed | 至少一个容器以失败退出且不自动重启 |
| Unknown | 无法获取状态（通常是与节点通信出错） |

> ⚠️ **phase 不是完整状态机**；细粒度状态由 `conditions` 数组表达，按顺序经历 `PodScheduled → PodReadyToStartContainers → Initialized → ContainersReady → Ready`，**Ready 为 True 才被加入 Service 负载均衡池**。

### 3. 容器状态与重启策略

每个容器有 **Waiting**（拉镜像/应用配置）、**Running**（执行中）、**Terminated**（已结束，含退出码）三种状态。Pod 级 `restartPolicy` 可取 `Always`（默认）/`OnFailure`/`Never`：退出码 0 时 OnFailure 不重启，非 0 时 Always 与 OnFailure 都重启，Never 永不重启。

> ⚠️ 连续崩溃时 kubelet 施加指数退避（**CrashLoopBackOff**），容器成功运行约 10 分钟后退避重置；**Deployment 只允许 `restartPolicy: Always`**，Job 常用 OnFailure/Never——这是两类工作负载的分水岭。

### 4. QoS 等级与驱逐优先级

Kubernetes 根据容器 requests/limits 的关系把 Pod 分为三类：

| QoS 等级 | 判定条件 | 驱逐优先级 |
|---------|---------|-----------|
| Guaranteed | 每个容器 CPU/内存 request=limit 且均大于 0 | 最低（最后被驱逐） |
| Burstable | 不满足 Guaranteed 但至少一个容器有 request 或 limit | 中间 |
| BestEffort | 没有任何 request/limit | 最高（最先被驱逐） |

> 💡 节点资源压力驱逐顺序固定为 **BestEffort → Burstable → Guaranteed**，且驱逐只针对**超过 requests** 的 Pod。Guaranteed Pod 可配合 static CPU 管理策略独占 CPU。

### 5. Deployment 三级控制链与 pod-template-hash

Deployment **不直接管理 Pod**，而是管理 ReplicaSet：每次 Pod 模板变更触发新 ReplicaSet 创建并滚动替换旧 ReplicaSet。Deployment 控制器给每个 ReplicaSet 打上 **pod-template-hash 标签**（PodTemplate 哈希值），保证子 ReplicaSet 选择器互不重叠。

> ⚠️ selector 创建后**不可变**（immutable）；只有 `.spec.template` 变更才触发 rollout 并产生新 revision，纯伸缩（如改 replicas）不产生 revision，因此**回滚只回滚 Pod 模板部分**。

### 6. 滚动更新约束：maxSurge / maxUnavailable 与比例伸缩

`RollingUpdate` 策略用两个约束控制滚动力学：

- **maxUnavailable**（默认 25%）：滚动期间最多不可用副本数
- **maxSurge**（默认 25%）：允许超出期望副本数的最大多余副本

若 rollout 进行中（含暂停）又收到伸缩请求，控制器按比例把新增副本分配到现有活跃 ReplicaSet 之间以降低风险，称为**比例伸缩（Proportional Scaling）**。

![[assets/k8s/diagram-k8s-rollout-flow.svg]]
*图：滚动更新与 rollover 流程——更新进行中再次更新会立即转向最新版本*

> ⚠️ 更新进行中再次更新触发 **rollover**：立即杀死已创建的旧版本 Pod 转向新版本，不等旧 rollout 完成；Deployment 对卡住的 rollout 仅报告 `Progressing=False / ProgressDeadlineExceeded` 状态，**不自动干预**。

### 7. Service 虚拟 IP 与 kube-proxy 转发链路

Service 是 Pod 集合的网络抽象：控制面从 `service-cluster-ip-range` 分配 ClusterIP（动态分配优先用上段，手工指定应选下段以降低冲突），EndpointSlice 控制器维护匹配选择器的端点；每个节点 kube-proxy 监听变化并安装转发规则，将发往 VIP 的流量 DNAT 到后端 Pod。

![[assets/k8s/diagram-k8s-service-forwarding.svg]]
*图：Service 转发链路——kube-proxy 规则 DNAT 到后端，Headless 模式由 DNS 直接返回 Pod 记录*

**Linux 三种代理模式对比**：

| 模式 | 机制 | 特点 | 现状 |
|------|------|------|------|
| iptables | 每 Service/端点数条规则，随机选后端 | 默认模式，规则数随规模膨胀 | 默认 |
| ipvs | 哈希表 | 性能好，但无法完整实现 Service 语义 | **v1.35 起弃用** |
| nftables | iptables 后继 | 性能更好 | 官方推荐替代 ipvs |

### 8. EndpointSlice 与无选择器 Service

**EndpointSlice**（v1.21 起 stable）把 Service 后端端点切片存储，每个切片默认满 **100 个端点**即新建切片，原生支持双栈与 `trafficDistribution` 等新特性，取代已废弃（v1.33 deprecated）的 Endpoints API。无选择器 Service 需手工创建 EndpointSlice 并打 `kubernetes.io/service-name` 标签关联，可用于抽象集群外后端。

> 💡 **Headless Service**（`clusterIP: None`）不分配 VIP、kube-proxy 不处理，DNS 直接返回后端 Pod 的 A/AAAA 记录，适合 StatefulSet 等需要直连 Pod 的场景。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|---------|---------|
| 容器反复崩溃进入 CrashLoopBackOff，Pod 无法 Ready | 应用错误、配置错误（环境变量/配置文件缺失）、资源不足（内存超限被杀）；kubelet 对连续崩溃施加指数退避 | `kubectl describe pod` 查看容器状态与 Reason；`kubectl logs` 检查应用日志；修正配置或申请更多资源。容器成功运行约 10 分钟后退避计数重置 | [pod-lifecycle#restart-policy](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#restart-policy) |
| 滚动更新卡住：`kubectl rollout status` 一直等待，新副本起不来 | 镜像拉取失败（tag 不存在/无权限）、就绪探针失败、配额不足、LimitRange 限制、运行时配置错误 | 设置 `.spec.progressDeadlineSeconds`（如 600）让控制器超时后报告 `Progressing=False / ProgressDeadlineExceeded`；检查 rollout status 与 describe 定位原因，修复后 `rollout undo` 回滚。K8s 对卡住的 rollout 本身不采取自动动作 | [deployment#failed-deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#failed-deployment) |
| 更新进行中再次更新，旧版本 Pod 被立即杀死（rollover） | Deployment 控制器发现新 Pod 模板后立即创建新 ReplicaSet 并转向它，正在扩容的旧 ReplicaSet 开始缩容——如 5 副本只建了 3 个就更新，3 个旧 Pod 立即被杀 | **这是设计行为而非缺陷**：多版本同时在线会放大风险。需要多版本灰度时使用 pause/resume 或金丝雀（canary）部署（多个 Deployment 对应不同版本） | [deployment#rollover](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#rollover) |
| 手工 `kubectl scale` 扩容后执行 `kubectl apply` 又被改回 manifest 里的副本数 | apply 把 manifest 中的 replicas 视为期望值覆盖手工扩容结果；HPA 管理伸缩时设置 spec.replicas 会与 HPA 冲突 | 纯手工伸缩不要同时用 apply 管理 replicas；若 HPA 在管理该 Deployment，**不要设置 `.spec.replicas`**，让控制面自动管理该字段 | [deployment#replicas](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#replicas) |
| 大规模集群（上万 Pod/Service）iptables 模式规则同步缓慢、更新延迟高 | iptables 为每个 Service 与每个端点 IP 都安装规则，万级规模下规则数以万计，变更时 kube-proxy 同步内核规则耗时很长 | 通过 kube-proxy 配置调整 `iptables.minSyncPeriod`（如 1s）与 `syncPeriod`（如 30s）聚合批量变更；或评估迁移 nftables 模式（性能优于 iptables 与 ipvs）。**ipvs 在 v1.35 已弃用，不建议新集群选用** | [virtual-ips#proxy-mode-iptables](https://kubernetes.io/docs/reference/networking/virtual-ips/#proxy-mode-iptables) |
| NodePort 手工指定端口与已分配端口冲突，Service 创建失败 | 默认 NodePort 范围 30000-32767，动态分配与手工指定共用同一池，可能互相碰撞 | 端口范围分两段：静态段 **30000-30085**（适合手工指定，冲突风险低）、动态段 **30086-32767**（自动分配默认使用，耗尽后才用静态段）。手工指定端口时从静态段选择 | [service#avoid-nodeport-collisions](https://kubernetes.io/docs/concepts/services-networking/service/#avoid-nodeport-collisions) |
| 无选择器 Service 执行 `kubectl port-forward service/xxx` 失败 | API server 不允许代理未映射到 Pod 的端点（如手工 EndpointSlice 指向集群外地址），防止 API server 被当作绕过授权的代理 | 无选择器 Service 用普通网络路径访问（集群内直连 VIP/NodePort）；若要转发到 Pod 必须让 Service 通过选择器关联真实 Pod。手工 EndpointSlice 的端点 IP 不能是 loopback/link-local 或其他 Service 的 ClusterIP（kube-proxy 不支持以 VIP 为目的地址） | [service#service-no-selector-access](https://kubernetes.io/docs/concepts/services-networking/service/#service-no-selector-access) |
| `externalTrafficPolicy: Local` 时部分节点访问 Service 无响应（流量被丢弃） | Local 策略只把外部流量转发到本节点上就绪的后端 Pod；某节点没有本地端点时 kube-proxy 直接丢弃该流量（internalTrafficPolicy: Local 同样如此） | 使用 Local 策略时确保后端 Pod 覆盖所有入口节点（如配合 DaemonSet 或节点亲和），或接受 Cluster 策略的额外一跳转发开销以换取全节点可用性 | [virtual-ips#traffic-policies](https://kubernetes.io/docs/reference/networking/virtual-ips/#traffic-policies) |

## 最佳实践

1. **用 Deployment 而非裸 Pod 或直接操作 ReplicaSet** — ReplicaSet 只保证副本数量，Deployment 额外提供声明式更新、滚动策略、回滚、暂停恢复；裸 Pod 在节点故障时不会自动恢复。官方明确建议：除非需要自定义更新编排，否则始终使用 Deployment。（[ReplicaSet](https://kubernetes.io/docs/concepts/workloads/controllers/replicaset/)）

2. **按云原生韧性设计：预期容器任意时刻被重启** — 设计上接受未通告的任意重启：要么让 Pod 失败并依赖控制器自动替换，要么做容器级韧性设计（优雅退出、快速启动、无状态化），保证部分故障下工作负载整体可用。（[pod-lifecycle#container-restart-resilience](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-restart-resilience)）

3. **提前规划 selector，创建后不可变** — Deployment 的标签选择器创建后不可修改（kubectl patch/edit/apply/helm upgrade 均不行）。修改 selector 只能删除重建 Deployment（默认级联删除 Pod 造成停机，可用 `--cascade=orphan` 保留）；selector 收窄会导致旧 ReplicaSet 被孤儿化并重建全新副本集。（[deployment#label-selector-updates](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#label-selector-updates)）

4. **HPA 管理伸缩时不要设置 spec.replicas** — 让控制面自动管理该字段，避免手工与自动伸缩互相覆盖。（[deployment#replicas](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#replicas)）

5. **为关键应用配置 requests=limits 获得 Guaranteed QoS** — Guaranteed 等级 Pod 驱逐优先级最低（BestEffort → Burstable → Guaranteed），且不会被随意抢占；生产关键业务应为每个容器设置相等的 CPU/内存 request 与 limit，这也是资源预算可预测的前提。（[pod-qos](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)）

6. **设置 progressDeadlineSeconds 及早发现卡住的发布** — 默认 Deployment 对停滞的 rollout 不采取任何动作。设置 `.spec.progressDeadlineSeconds`（如 600）让控制器超时后写入 `Progressing=False / ProgressDeadlineExceeded` 条件，供 CI/CD 或监控系统据此告警与回滚。（[deployment#failed-deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#failed-deployment)）

7. **服务发现优先用 DNS 而非环境变量** — 环境变量方式（`{SVCNAME}_SERVICE_HOST/PORT`）只在 Pod 创建时注入，Service 必须先于客户端 Pod 存在，否则变量缺失；DNS（CoreDNS）无此顺序依赖，且支持跨命名空间（`my-service.my-ns`）解析，是推荐的服务发现方式。（[service#discovering-services](https://kubernetes.io/docs/concepts/services-networking/service/#discovering-services)）

8. **客户端优先使用 EndpointSlice API 而非废弃的 Endpoints** — Endpoints API 在 v1.33 已标记废弃：不支持双栈、不含 trafficDistribution 等新特性信息、超 1000 端点会截断（over-capacity）。所有客户端应迁移到 EndpointSlice。（[service#endpoints](https://kubernetes.io/docs/concepts/services-networking/service/#endpoints)）

## 排查命令

```bash
# Pod 生命周期与容器状态
kubectl get pod <pod> -o wide                    # 查看 phase、节点、IP
kubectl describe pod <pod>                       # conditions、容器状态、Events
kubectl get pod <pod> -o jsonpath='{.status.conditions[*].type}'  # 查看 conditions 序列

# CrashLoopBackOff 排查
kubectl logs <pod> --previous                    # 查看上次崩溃的容器日志
kubectl describe pod <pod> | grep -A5 "State:"   # 退出码与 Reason

# QoS 等级查看
kubectl get pod <pod> -o jsonpath='{.status.qosClass}'

# Deployment 滚动更新
kubectl rollout status deployment/<name>         # 等待滚动完成
kubectl rollout history deployment/<name>        # revision 历史
kubectl rollout undo deployment/<name> --to-revision=N
kubectl rollout pause deployment/<name>          # 暂停（配合多版本灰度）
kubectl rollout resume deployment/<name>
kubectl get rs -l pod-template-hash              # 查看各 ReplicaSet 哈希

# Service 转发链路排查
kubectl get svc <name> -o yaml                   # ClusterIP、selector、端口
kubectl get endpointslices -l kubernetes.io/service-name=<name>  # 端点切片
kubectl get svc <name> -o jsonpath='{.spec.clusterIP}'
# 节点上查看代理规则（需登录节点）
iptables -t nat -L KUBE-SERVICES | grep <svc>    # iptables 模式
nft list ruleset | grep <svc>                    # nftables 模式
```

## 官方参考文档

- [Pod Lifecycle（生命周期状态机/重启策略/优雅终止）](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
- [Pod Conditions（生命周期条件与自定义 readiness gate）](https://kubernetes.io/docs/concepts/workloads/pods/pod-condition/)
- [Pod Quality of Service Classes（QoS 分级与驱逐优先级）](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)
- [Deployment（滚动更新/比例伸缩/回滚/状态条件）](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [ReplicaSet（工作方式与使用建议）](https://kubernetes.io/docs/concepts/workloads/controllers/replicaset/)
- [Service（类型/无选择器/Headless/发现机制）](https://kubernetes.io/docs/concepts/services-networking/service/)
- [Virtual IPs and Service Proxies（kube-proxy 模式/会话亲和/流量策略）](https://kubernetes.io/docs/reference/networking/virtual-ips/)
- [EndpointSlices（端点切片 API）](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/)

## 相关笔记

- [[k8s-pod-deployment-service]] — K8s 核心概念**基础版**（是什么/四种 Service 类型/探针/通用排查），本篇为其设计原理深度补充，两篇互链对照阅读
- [[k8s-cluster-troubleshooting]] — K8s 集群排障实战（本篇「常见问题表」的原理在此落地）
- [[k8s-cluster-ops-combat]] — K8s 集群运维实战
- [[k8s-ops-essentials]] — K8s 运维基础与集群管理
