---
created: 2026-08-03
source: Kubernetes Official Docs / kube-prometheus / Prometheus Docs
topic: K8s实战
priority: 🔴最高
theme: k8s
---

# K8s实战 — 集群排错、etcd 备份恢复、监控体系与节点维护

## 概述

生产集群运维四大核心场景：**集群排错实战**（Node 异常、kubectl 连接、TLS 证书）、**etcd 备份与恢复**（快照、恢复、成员替换、碎片整理）、**监控体系**（metrics-server 资源指标管道、kube-prometheus 全栈监控、Prometheus 规则最佳实践）、**节点维护**（kubectl drain 安全驱逐与 PodDisruptionBudget）。

核心方法论：先定位症状（Node Condition / Event / 日志），再按**故障模式（failure mode）**排查；配合定期备份与监控预警，实现**可恢复、可观测**的生产集群。

## 架构图

### 排错与运维视角的 K8s 架构
![[assets/k8s/diagram-k8s-troubleshooting-arch.svg]]

### etcd 备份与恢复流程
![[assets/k8s/diagram-etcd-backup-restore-flow.svg]]

### 节点维护（drain / PDB）流程
![[assets/k8s/diagram-node-drain-flow.svg]]

## 核心概念

### 1. Node Condition 与 Ready 状态

kubelet 定期上报**心跳（Lease）**与节点状态（Ready / MemoryPressure / DiskPressure / PIDPressure / NetworkUnavailable）。kubelet 停止上报后节点变为 `NotReady` / `Unknown`，**Pod 在 NotReady 5 分钟后被驱逐**。

> **关键要点**：`kubectl describe node` 查看 Conditions 与 Events 是定位节点问题的第一步。

### 2. kubectl drain / Eviction API

`kubectl drain` 安全驱逐节点上所有 Pod（尊重优雅终止期与 PDB）；编程方式可调用 `policy/v1` 的 **Eviction API** 获得更细粒度控制。

> **关键要点**：DaemonSet Pod 需加 `--ignore-daemonsets`；维护完成后 `kubectl uncordon` 恢复调度。

### 3. PodDisruptionBudget (PDB)

限制**自愿中断**（drain / 节点维护）时同时不可用的 Pod 数量（`minAvailable` / `maxUnavailable`），保障维护期间应用可用性。

> **关键要点**：建议将 `Unhealthy Pod Eviction Policy` 设为 `AlwaysAllow`，避免异常 Pod 阻塞节点维护。

### 4. etcd 快照备份

`etcdctl snapshot save` 从在线成员创建一致性快照，或复制未被进程使用的 `member/snap/db` 文件；快照包含全部 Kubernetes 状态与关键信息。

> **关键要点**：快照文件需**加密存放**；用 `etcdutl snapshot status` 验证快照完整性（`etcdctl snapshot status` 已废弃）。

### 5. etcd 恢复（etcdutl snapshot restore）

恢复前**必须先停止全部 API server 实例**；`etcdutl --data-dir <目录> snapshot restore snapshot.db` 恢复到新目录，修改 etcd.yaml 的 hostPath 后重启 etcd。

> **关键要点**：多数成员永久故障时集群视为失败，无法写入新状态，**只能从快照恢复**。

### 6. 资源指标管道（metrics-server）

metrics-server 发现所有节点并查询 kubelet 的 CPU/内存用量（kubelet 经 CRI 从容器运行时或 cAdvisor 获取），暴露 `metrics.k8s.io` API。

> **关键要点**：轻量、短期、**内存态**指标，驱动 `kubectl top` 与 HPA。

### 7. kube-prometheus 监控栈

Prometheus Operator + 高可用 Prometheus + Alertmanager + node-exporter + kube-state-metrics + Grafana + prometheus-adapter，开箱即用的端到端集群监控。

> **关键要点**：依赖 kubelet 开启 token 认证（`--authentication-token-webhook=true`）与 Webhook 授权（`--authorization-mode=Webhook`）。

### 8. Node Problem Detector (NPD)

节点健康守护进程（DaemonSet），通过 SystemLogMonitor / SystemStatsMonitor / CustomPluginMonitor / HealthChecker 检测内核与运行时问题，由 Kubernetes exporter 上报为 **Node Condition 或 Event**。

> **关键要点**：临时问题上报为 Event，永久问题上报为 Node Condition；官方建议**全集群部署**。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|---------|---------|
| 节点 NotReady，Pod 被驱逐 | kubelet 故障或网络断开，停止上报节点状态（Lease 过期） | `kubectl describe node` 查看 Events；登入节点检查 kubelet 服务与 `/var/log/kubelet.log`（或 `journalctl -u kubelet`） | [Troubleshooting Clusters](https://kubernetes.io/docs/tasks/debug/debug-cluster/) |
| `kubectl` 报 `Unable to connect ... i/o timeout` | kubeconfig 缺失/无效、context 选错、VPN 断开或 API server 不可达 | 检查 `~/.kube/config` 与 `$KUBECONFIG`；`kubectl config get-contexts` 切换；ping API server 检查网络/防火墙/LB | [Troubleshoot kubectl](https://kubernetes.io/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/) |
| kubectl 连接报 TLS 证书问题 | 客户端证书或 CA 证书过期、信任链无效 | `kubectl config view --flatten` 提取证书，base64 解码后 `openssl x509 -noout -dates` 检查有效期；过期则重新签发 | [Troubleshoot kubectl](https://kubernetes.io/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/) |
| etcd 单个成员故障 | 成员节点宕机、磁盘或网络问题 | `etcdctl member list` → `member remove <ID>` → `member add 新成员 --peer-urls` → `ETCD_INITIAL_CLUSTER_STATE=existing` 启动；多成员故障逐个替换并同步 API server 的 `--etcd-servers` | [Operating etcd](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/) |
| etcd 多数成员永久故障，集群无法写入 | 超过半数成员丢失，etcd 失去 quorum，leader 无法选举 | 停止全部 API server → `etcdutl snapshot restore` → 修改 etcd.yaml hostPath → 重启 kubelet 与全部控制面组件 | [Operating etcd](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/) |
| Prometheus 抓取 kubelet 报 401/403 | kubelet 未开启 token 认证（403）或 Webhook 授权（401） | 开启 `--authentication-token-webhook=true` 与 `--authorization-mode=Webhook` | [kube-prometheus troubleshooting](https://github.com/prometheus-operator/kube-prometheus/blob/main/docs/troubleshooting.md) |
| Prometheus 抓不到 kube-proxy 指标 | kubeadm 默认将 metricsBindAddress 绑为 127.0.0.1 | 修改 kube-proxy ConfigMap 的 `metricsBindAddress` 为 `0.0.0.0:10249`，`kubectl -n kube-system rollout restart daemonset kube-proxy` | [kube-prometheus troubleshooting](https://github.com/prometheus-operator/kube-prometheus/blob/main/docs/troubleshooting.md) |
| 装 kube-prometheus 报 APIService `v1beta1.metrics.k8s.io already exists` | 已有 metrics-server，与 prometheus-adapter 冲突 | 二选一：卸载 metrics-server，或配置 `common.resourceMetricsAPI` 改走轻量方案 | [kube-prometheus troubleshooting](https://github.com/prometheus-operator/kube-prometheus/blob/main/docs/troubleshooting.md) |
| kubectl drain 卡住无法完成 | Pod 无 PDB 保护或 PDB 不允许驱逐（健康副本不足），或存在无法优雅终止的 Pod | 为关键应用配置 PDB 并将 Unhealthy Eviction Policy 设 AlwaysAllow；加 `--ignore-daemonsets`；必要时评估 `--force`；多节点可并行 drain | [Safely Drain a Node](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/) |

## 最佳实践

### 1. 生产 etcd 采用静态五成员集群并定期备份

官方强烈建议生产集群始终运行**静态 5 成员** etcd 集群（3 成员为最小高可用），不要为 etcd 配置自动扩缩；定期 `etcdctl snapshot save` 备份、`etcdutl snapshot status` 验证，快照加密存放；**升级 etcd 前必须先备份**。

> 参考：[Operating etcd clusters](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/)

### 2. etcd 定期碎片整理（defragmentation）

碎片整理开销大，应尽量**低频执行**，但要保证成员不超过存储配额；官方推荐 `etcd-defrag` 工具，可部署为 **Kubernetes CronJob** 定期自动执行。

> 参考：[ahrtr/etcd-defrag](https://github.com/ahrtr/etcd-defrag)

### 3. 节点维护前配置 PodDisruptionBudget

drain 前为需要高可用的工作负载配置 PDB（`minAvailable` / `maxUnavailable`），并建议将 `Unhealthy Pod Eviction Policy` 设为 `AlwaysAllow`，避免异常应用阻塞节点维护；drain 成功后维护完记得 **uncordon** 恢复调度。

> 参考：[Safely Drain a Node](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/)

### 4. 监控前置条件：kubelet token 认证与 Webhook 授权

kube-prometheus 默认假设 kubelet 使用 **ServiceAccount token 认证 + Webhook 授权**，比客户端证书方式权限更细粒度、更易管控；未开启时 Prometheus 抓取 kubelet 会 401/403。

> 参考：[kube-prometheus](https://github.com/prometheus-operator/kube-prometheus)

### 5. Recording rules 命名规范 `level:metric:operations`

记录规则命名采用 **层级:指标:操作** 形式，`rate()` 时去掉计数器 `_total` 后缀；聚合比率时**先分别聚合分子分母再相除**，不要对比率或平均值再求平均；聚合时始终用 `without` 指定去掉的标签，保留 job 等有用标签。

> 参考：[Prometheus Recording rules](https://prometheus.io/docs/practices/rules/)

### 6. 告警哲学：针对症状、保持简洁

尽可能**少而精**的告警，针对终端用户体验相关的症状而非穷举所有可能原因；同一调用链只在一点对延迟分页；离线处理关注数据流转时长，批处理任务给足两次运行间隔的余量；用**黑盒探测**补充白盒监控，并**监控监控本身**（metamonitoring）；告警名社区惯例使用 **Camel Case**。

> 参考：[Prometheus Alerting](https://prometheus.io/docs/practices/alerting/)

### 7. 识别集群故障模式并分层缓解

官方故障模式清单：API server 宕机**不影响已运行 Pod**；etcd 存储丢失需手工恢复；节点宕机由控制器在其他节点重建 Pod。缓解手段：IaaS 自动重启、可靠持久化存储、HA 控制面、定期快照 apiserver 磁盘、应用设计为容忍重启。

> 参考：[Troubleshooting Clusters](https://kubernetes.io/docs/tasks/debug/debug-cluster/)

### 8. 全集群部署 Node Problem Detector

官方建议在集群中运行 NPD 监控节点健康，资源开销可控（内核日志增长缓慢 + 资源限制）；支持内核日志、systemd、自定义插件等多类 problem daemon，Prometheus exporter 可将节点问题输出为指标。

> 参考：[Monitor Node Health](https://kubernetes.io/docs/tasks/debug/debug-cluster/monitor-node-health/)

## 排查命令速查

```bash
# 节点状态与事件
kubectl get nodes
kubectl describe node <node>

# kubelet 日志
journalctl -u kubelet
tail -f /var/log/kubelet.log

# kubectl 连接排错
kubectl config get-contexts
kubectl config view --flatten
kubectl config use-context <context>

# 证书有效期检查
kubectl config view --flatten -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d | openssl x509 -noout -dates

# etcd 备份与验证
etcdctl snapshot save snapshot.db
etcdutl snapshot status snapshot.db

# etcd 成员管理
etcdctl member list
etcdctl member remove <ID>
etcdctl member add <name> --peer-urls=https://<ip>:2380

# etcd 恢复
etcdutl --data-dir /var/lib/etcd-new snapshot restore snapshot.db

# 节点维护
kubectl drain <node> --ignore-daemonsets
kubectl uncordon <node>

# 监控
kubectl top node
kubectl top pod -A
```

## 相关笔记

- [[k8s-cluster-ops-combat]] — 集群运维实战基础（升级、排错方法论、监控搭建）
- [[k8s-pod-deployment-service]] — Pod / Deployment / Service 核心概念
- [[k8s-etcd-backup-restore]] — etcd 备份恢复专题
- [[k8s-monitoring-pipeline]] — 监控管道专题
