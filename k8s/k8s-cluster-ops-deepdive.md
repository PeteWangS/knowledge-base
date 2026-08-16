---
created: 2026-08-16
source: Kubernetes Official Docs / Prometheus Docs / etcd Docs
topic: K8s实战
priority: 🔴最高
theme: k8s
---

# K8s实战 — 集群运维深度：Pod 排错、etcd 备份恢复、Prometheus 监控与节点维护

## 概述

生产集群运维四大高频场景的**官方文档细节深度**：① **集群排错**——Pod 状态机诊断（Pending/Waiting/Terminating/CrashLoopBackOff）、kubectl 连接链路排错（kubeconfig/证书/上下文）、termination message 定位容器失败根因；② **etcd 备份恢复**——etcdctl/etcdutl 分工、快照备份/校验/灾难恢复、碎片整理与配额管理；③ **监控体系**——资源指标管线（metrics-server/metrics.k8s.io）与完整指标管线（Prometheus/custom.metrics.k8s.io）双层架构、Prometheus 指标类型与告警/记录规则实践、Grafana 集成、Node Problem Detector 节点健康监控；④ **节点维护**——PodDisruptionBudget 约束下的 kubectl drain/cordon/uncordon 安全排空。

所有结论均出自 Kubernetes 官方文档（tasks/debug、tasks/administer-cluster）与 Prometheus 官方文档（practices、operating、concepts、visualization），**可直接用于生产排障**。

> 与 [[k8s-cluster-troubleshooting]] 的差异：该篇为基础方法论与故障模式总览，本篇聚焦官方文档的命令级细节、指标类型原理与双层管线架构。

## 架构图

### 可观测性 / 运维体系分层架构

![[assets/k8s/diagram-k8s-ops-deepdive-arch.svg]]

双层指标管线 + 节点健康检测（NPD）+ 数据面 etcd 运维闭环 + 告警链路的分层关系：资源指标管线服务 kubectl top 与 HPA 基础指标；完整指标管线经适配器扩展 custom/external 指标；NPD 将节点问题上报为 Events / Node Conditions 或 OpenMetrics 指标；etcd 备份→校验→恢复→碎片整理构成灾难恢复闭环。

## 核心概念

### 1. Pod 状态机与排错入口

Pod 生命周期状态：**Pending**（已创建未调度）、**Waiting**（已调度但容器无法运行，最常见是拉镜像失败）、**Running**、**Terminating**（删除已发出但 finalizer/admission webhook 阻止清理）、**CrashLoopBackOff**（反复崩溃重启）。

排错第一步是 **triage**：判断问题在 Pod、ReplicationController 还是 Service 层，用 `kubectl describe pods` 查看状态、调度器消息与事件。

> **关键要点**：describe 输出中的调度器消息与容器状态是定位根因的第一手证据；Terminating 卡住要查针对 pods **UPDATE 操作**的 Validating/MutatingWebhookConfiguration。

*图：Pod 状态机诊断流程*

![[assets/k8s/diagram-pod-debug-flow.svg]]

### 2. etcd 快照备份与恢复（etcdctl / etcdutl）

**etcdctl 是网络客户端**（日常运维、member list、snapshot save）；**etcdutl 直接操作数据文件**（snapshot status/restore、defragmentation、版本迁移）。备份方式：内置快照（`etcdctl snapshot save` 不影响成员性能）或拷贝未运行的 member/snap/db 文件或存储卷快照。恢复：`etcdutl --data-dir <dir> snapshot restore snapshot.db`。

> **关键要点**：`etcdctl snapshot status/restore` 自 v3.5.x 起**弃用**、v3.6 移除，官方推荐 etcdutl；恢复前**必须停止全部 API server**，恢复后建议重启 kube-scheduler / kube-controller-manager / kubelet 避免陈旧数据。

*图：etcd 备份 → 校验 → 恢复 → 碎片整理运维闭环*

![[assets/k8s/diagram-etcd-ops-cycle.svg]]

### 3. 节点排空 kubectl drain / cordon / uncordon

`kubectl drain` 安全驱逐节点上全部 Pod（尊重优雅终止期与 PodDisruptionBudget），返回成功后即可下电/删机；**DaemonSet Pod 不会被 drain 驱逐**（DaemonSet 控制器立即重建），需 `--ignore-daemonsets`；维护完成后 `kubectl uncordon` 恢复调度。

> **关键要点**：drain 只对单节点执行、多节点可在不同终端并行且仍遵守 PDB；PDB 建议设 `unhealthyPodEvictionPolicy: AlwaysAllow` 允许驱逐不健康应用；避免容忍 `node.kubernetes.io/unschedulable` 污点（DaemonSet 除外）。

### 4. Node Problem Detector（节点问题检测）

NPD 是官方节点健康监控组件，由 problem daemon 组成：**SystemLogMonitor**（内核日志/kmsg/journald/filelog 按规则报问题）、**SystemStatsMonitor**（采集健康统计指标）、**CustomPluginMonitor**（运行用户脚本按退出码/stdout 协议检测）、**HealthChecker**（检查 kubelet 与容器运行时）。

> **关键要点**：exporter 决定上报去向——Kubernetes exporter 将**临时问题报为 Events、永久问题报为 Node Conditions**；Prometheus exporter 本地暴露 OpenMetrics 指标；配置可用 ConfigMap 覆盖（仅 kubectl 启动方式支持）。

### 5. 资源指标管线 vs 完整指标管线

**资源指标管线**：metrics-server 采集 kubelet CPU/内存 → metrics.k8s.io API → kubectl top 与 HPA，轻量内存态；**完整指标管线**：监控系统抓取 kubelet 与 exporter → 适配器实现 custom.metrics.k8s.io / external.metrics.k8s.io → HPA 自定义/外部指标扩缩容。K8s 设计面向 **OpenMetrics**（CNCF 观测项目，Prometheus exposition format 的向后兼容扩展）。

> **关键要点**：kubectl top 无数据先查 metrics-server；自定义指标扩缩容必须实现 custom/external.metrics.k8s.io **适配器**，仅靠 Prometheus 抓取不够。

### 6. Prometheus 指标类型与规则命名

四种指标类型：**Counter**（单调递增计数，重启归零，如请求数/错误数，不可表示可减的值）、**Gauge**（可上下波动，如当前内存/并发请求）、**Histogram**（桶计数观测分布，_bucket/_sum/_count，含 native 与 classic 两种）、**Summary**（滑动窗口分位数 quantile + _sum + _count）。

记录规则命名 **level:metric:operations**；告警规则**对症状告警而非穷举原因**。

> **关键要点**：Histogram 与 Summary 取舍看分位数可聚合性——Summary 的 quantile **不可跨实例聚合**，Histogram 的 _bucket 可聚合（rate() 后）。

### 7. termination message（容器终止消息）

Kubernetes 从容器终止消息文件（默认 /dev/termination-log）读取内容填充 status 的 `terminated.message`，用于容器失败根因定位。可用 `terminationMessagePath` 自定义路径，`terminationMessagePolicy: FallbackToLogsOnError` 在文件为空且错误退出时回退到日志尾部。

> **关键要点**：单容器消息上限 **4096 字节**；全容器总量 12KiB 均分；terminationMessagePath 启动后不可修改；多容器 Pod 用 Go template 按容器名过滤定位失败容器。

### 8. kubectl 连接链路排错

kubectl 无法连接集群的排查链路：`kubectl version` 确认客户端/服务端版本 → `~/.kube/config` 与 `$KUBECONFIG` 有效性 → VPN/网络连通性 → token 认证与 RBAC 授权 → `kubectl config get-contexts / use-context` 上下文 → API server 与负载均衡器可达性（ping/防火墙/云健康检查）→ TLS 证书过期。

> **关键要点**：`kubectl config view --flatten` + base64 -d + `openssl x509 -noout -dates` 检查证书有效期；**client-certificate 过期是常见根因**。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|---------|---------|
| Pod 一直处于 Pending 无法调度 | 集群 CPU/内存资源不足（requests 超出可分配量），或 Pod 绑定 hostPort 导致可调度位置受限（最多与节点数相同） | kubectl describe pods 查看调度器消息：资源不足则删除多余 Pod、调低 resource requests 或扩容节点；hostPort 场景优先改用 Service 暴露，确需 hostPort 则只能调度与节点数等量的 Pod | K8s 官方 debug-pods.md：My pod stays pending |
| Pod 卡在 Waiting / ImagePullBackOff | 镜像名写错、镜像未推送到 registry、或镜像拉取失败（网络/权限/私有仓库认证） | kubectl describe 确认镜像事件；核对镜像名与 registry；手动 docker pull 验证可拉取；私有仓库检查 imagePullSecrets | K8s 官方 debug-pods.md：My pod stays waiting |
| Pod 卡在 Terminating 无法删除 | Pod 带 finalizer，且集群中存在针对 pods UPDATE 操作的 admission webhook 阻止控制面移除 finalizer | 检查集群内针对 pods UPDATE 的 webhook：第三方 webhook 升级到最新版、禁用其对 UPDATE 的拦截或向厂商报 issue；自研 mutating webhook 不得在 UPDATE 时改动不可变字段（如 containers） | K8s 官方 debug-pods.md：My pod stays terminating |
| Service 无 Endpoints，访问不到后端 | Service 的 selector 标签与 Pod 标签不匹配，或 Pod 的 containerPort 与 Service 的 targetPort 不一致 | kubectl get endpointslices -l kubernetes.io/service-name= 核对端点数；kubectl get pods --selector= 验证标签匹配；检查 containerPort 与 targetPort 对应关系 | K8s 官方 debug-pods.md：My service is missing endpoints |
| kubectl 报 Unable to connect ... i/o timeout | kubeconfig 无效/丢失、$KUBECONFIG 未配置、VPN 断开、context 指向错误集群、API server 或前端 LB 不可达、客户端证书过期 | 按链路逐级排查：kubectl version → 检查 ~/.kube/config 或从控制面复制 admin.conf → kubectl config get-contexts/use-context → ping API server、查防火墙与云健康检查 → kubectl config view --flatten 导出证书用 openssl x509 -noout -dates 验证有效期 | K8s 官方 troubleshoot-kubectl.md |
| etcd 集群无 leader、状态不稳定 | 资源饥饿（网络抖动、磁盘 I/O 饱和）导致 leader 心跳超时；etcd 是 leader-based 分布式系统，心跳超时后无 leader 选举成功，集群无法变更状态 | etcd 运行在专用机器或隔离环境保证资源；生产保持奇数成员（官方推荐五成员）；网络/磁盘性能敏感，避免资源饥饿；生产最低版本 3.4.29+ / 3.5.11+ | K8s 官方 configure-upgrade-etcd.md |
| etcd 存储配额超限（database size exceeded） | etcd 默认配额（约 2GB）内历史键版本持续堆积，数据库膨胀超限后集群进入只读模式拒绝写入 | 执行碎片整理 defragmentation（官方推荐 etcd-defrag 工具，可作 CronJob 定期执行）；压缩历史版本（compact）；碎片整理开销大应尽量低频、在低峰执行 | K8s 官方 configure-upgrade-etcd.md |
| kubectl drain 卡住无法完成节点排空 | PDB 的 minAvailable 阻止驱逐（健康副本数会跌破预算）、未指定 --ignore-daemonsets（DaemonSet 控制器立即重建导致驱逐循环）、或不健康应用 Pod 等待变健康才允许驱逐 | 配置 PDB 并设 unhealthyPodEvictionPolicy: AlwaysAllow；drain 时加 --ignore-daemonsets；检查 PDB 健康副本数；drain 成功后维护完执行 kubectl uncordon | K8s 官方 safely-drain-node.md |
| 容器异常退出但 describe 看不到失败原因 | 容器未写入 termination message，或 message 为空且策略为默认 File 模式，根因只存在于容器日志中 | kubectl get pod -o go-template 过滤 .status.containerStatuses[].lastState.terminated.message；多容器 Pod 按容器名过滤；设置 terminationMessagePolicy: FallbackToLogsOnError（限 2048 字节或 80 行） | K8s 官方 determine-reason-pod-failure.md |
| 节点故障（内核问题/磁盘错误/kubelet 异常）无感知 | 集群未部署节点级健康检测，kubelet 心跳正常但节点自身已异常，问题被掩盖 | 部署 Node Problem Detector（DaemonSet）：SystemLogMonitor 检测内核日志、HealthChecker 检查 kubelet 与运行时；Kubernetes exporter 报 Events/Node Conditions，或 Prometheus exporter 接监控告警 | K8s 官方 monitor-node-health.md |
| Prometheus 告警噪声大，或记录规则聚合结果失真 | 对原因而非症状告警、栈的多层重复告警延迟、比率聚合时对比率取平均（统计无效）、聚合时用 by 而非 without 丢失 job 维度、记录规则命名不规范 | 只对端到端症状告警、延迟只在栈的一个点 page、批处理任务容忍至少 2 次运行失败再告警、用 metamonitoring 监控监控系统本身；记录规则命名 level:metric:operations，聚合比率先分别聚合分子分母再除，聚合指定 without 子句保留 job 标签 | Prometheus 官方 practices/alerting.md 与 practices/rules.md |

## 最佳实践

### 1. 排错先 triage 分类再动手

先判断问题属于 Pod、ReplicationController 还是 Service 层；`kubectl describe pods` 查看状态与事件是第一入口；用 `--validate` 校验清单、对比 apiserver 上的 Pod YAML 与本地清单，找出被静默忽略的字段拼写错误（如 command 写成 commnd）。

> 参考：[Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)

### 2. etcd 生产部署与备份基线

生产最低版本 3.4.29+ / 3.5.11+；奇数成员、官方推荐**静态五成员集群**（任何规模都不建议自动扩缩 etcd）；运行于专用机器保证资源；定期 `etcdctl snapshot save` 备份并**加密快照**（快照含全部 K8s 状态）；用 `etcdutl snapshot status` 校验快照有效性；**升级前必须先备份**。

> 参考：[Operating etcd clusters for Kubernetes](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/)

### 3. etcd 恢复严格流程：先停 API server 再恢复

任何 API server 运行中不得尝试恢复 etcd：先停止全部 API server 实例 → 在所有 etcd 实例恢复状态 → 重启全部 API server；恢复后建议重启 kube-scheduler、kube-controller-manager、kubelet 避免依赖陈旧数据；恢复集群访问 URL 变更时须重配 `--etcd-servers`。

> 参考：[Restoring an etcd cluster](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/)

### 4. 节点维护标准化流程

维护前为关键应用配置 PodDisruptionBudget（`unhealthyPodEvictionPolicy: AlwaysAllow`）→ `kubectl drain --ignore-daemonsets <node>` 单节点串行（多节点可在不同终端并行且仍遵守 PDB）→ 维护 → `kubectl uncordon` 恢复调度；避免其他对象容忍 `node.kubernetes.io/unschedulable` 污点，防止 Pod 排到已 drain 节点。

> 参考：[Safely Drain a Node](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/)

### 5. 监控体系双层管线分工

资源指标管线（metrics-server + metrics.k8s.io）负责 kubectl top 与 HPA 基础指标，轻量内存态；完整指标管线（Prometheus/OpenMetrics + custom/external.metrics.k8s.io 适配器）提供富指标与自定义扩缩容；建议集群部署 Node Problem Detector 监控节点健康（内核日志/系统统计/自定义插件/kubelet 与运行时健康检查）。

> 参考：[Tools for Monitoring Resources](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-usage-monitoring/) 与 [Monitor Node Health](https://kubernetes.io/docs/tasks/debug/debug-cluster/monitor-node-health/)

### 6. 告警少而准：对症状告警

尽可能少告警：对与终端用户体验相关的**症状**告警，而非穷举每种可能成因；在线服务只在高栈层的一个点对延迟告警，错误率对用户可见错误告警；离线处理对数据流转时长告警；批处理任务至少容忍 2 次完整运行失败；容量告警提前干预；**metamonitoring** 确保 Prometheus/Alertmanager 自身可用（黑盒测试优于逐组件白盒告警）。

> 参考：[Prometheus Alerting](https://prometheus.io/docs/practices/alerting/)

### 7. 记录规则命名与聚合规范

记录规则命名 **level:metric:operations**（聚合层:指标名:操作，最新操作在前），rate() 时去掉 _total 后缀，无明确操作时用 sum，比率用 _per_ 与 ratio；聚合比率先分别聚合分子分母再相除，**禁止对比率/平均数再取平均**；聚合始终用 without 子句保留 job 等维度避免冲突。

> 参考：[Prometheus Recording rules](https://prometheus.io/docs/practices/rules/)

### 8. Prometheus 安全基线

按安全模型假设 HTTP 端点与日志暴露给不可信用户：`--web.enable-admin-api` 与 `--web.enable-lifecycle` 默认关闭（开启后反向代理需拦截 /api/*/admin/、/-/reload、/-/quit 防 CSRF）；TLS 客户端证书认证与 Basic Auth（bcrypt 存储）加固；secret 字段不得放入会被 HTTP 端点暴露的普通配置项；Grafana 仪表盘权限不等于数据源权限，代理模式下勿以此限制查询；防止 PromQL 注入（转义不可信输入）。

> 参考：[Prometheus Security](https://prometheus.io/docs/operating/security/)

### 9. Grafana 接入 Prometheus

数据源：Configuration → Data Sources → Add data source → Prometheus → 填 URL（如 http://localhost:9090/）→ Save & Test；查询优先用 `$__rate_interval` 变量（Grafana 7.2+ 推荐）配合 rate/increase；导入 Grafana.com 共享仪表盘后需手动编辑 JSON 修正 datasource 名称；默认登录 admin/admin。

> 参考：[Prometheus Grafana visualization](https://prometheus.io/docs/visualization/grafana/)

## 排查命令

```bash
# Pod 排错：状态与事件
kubectl get pods
kubectl describe pods <pod>
kubectl get pod <pod> -o go-template='{{range .status.containerStatuses}}{{.name}}: {{.lastState.terminated.message}}{{end}}'

# kubectl 连接链路排错
kubectl version
kubectl config get-contexts
kubectl config use-context <context>
kubectl config view --flatten
# 证书有效期检查
kubectl config view --flatten -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d | openssl x509 -noout -dates

# Service endpoints 检查
kubectl get endpointslices -l kubernetes.io/service-name=<svc>

# etcd 备份与校验（etcdctl 网络客户端 / etcdutl 数据文件）
etcdctl snapshot save snapshot.db
etcdutl snapshot status snapshot.db

# etcd 恢复（先停止全部 API server）
etcdutl --data-dir /var/lib/etcd-new snapshot restore snapshot.db

# 节点维护
kubectl drain <node> --ignore-daemonsets
kubectl uncordon <node>

# 监控
kubectl top node
kubectl top pod -A
```

## 相关笔记

- [[k8s-cluster-troubleshooting]] — 排错方法论与故障模式总览（基础篇，本篇为官方文档细节深度，两者互补）
- [[k8s-cluster-ops-combat]] — 集群运维实战基础（升级、排错方法论、监控搭建）
- [[k8s-pod-deployment-service]] — Pod / Deployment / Service 核心概念
- [[k8s-core-objects-design]] — K8s 核心对象设计原理
