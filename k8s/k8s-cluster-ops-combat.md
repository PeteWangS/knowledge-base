---
created: 2026-07-26
source: GitHub Official Repos / Community
topic: K8s实战
priority: 🔴最高
theme: k8s
---

# K8s实战 — 集群排错、etcd 备份恢复、监控体系与节点维护

## 概述

Kubernetes 实战涵盖四大核心运维领域：集群问题排错、etcd 数据备份与恢复、Prometheus + Grafana 监控体系搭建、以及节点维护操作。这些是 Kubernetes 生产集群运维人员必须掌握的实战技能，直接关系到集群的可用性与数据安全。

## 架构图

### 集群运维四层架构
![[assets/k8s/diagram-k8s-ops-architecture.svg]]

### etcd 备份恢复流程
![[assets/k8s/diagram-etcd-backup-restore.svg]]

### Prometheus/Grafana 监控体系
![[assets/k8s/diagram-monitoring-pipeline.svg]]

## 核心概念

### etcd 集群操作

etcd 是 Kubernetes 的键值存储数据库，存储所有集群状态。操作 etcd 包括启动集群、配置安全通信、创建快照备份、从快照恢复、替换故障成员、扩容/缩容、碎片整理和升级等。

> **关键要点**：生产环境推荐 5 成员 etcd 集群，奇数成员可容忍 (N-1)/2 个节点故障。`etcdctl` 用于网络操作，`etcdutl` 用于操作数据文件。备份优先使用内置快照（`etcdctl snapshot save`），恢复使用 `etcdutl snapshot restore`。

### 节点维护（Drain / Cordon / Uncordon）

| 操作 | 命令 | 作用 |
|------|------|------|
| Drain | `kubectl drain <node>` | 安全驱逐 Pod + 标记不可调度 |
| Cordon | `kubectl cordon <node>` | 标记不可调度（不驱逐已有 Pod） |
| Uncordon | `kubectl uncordon <node>` | 恢复调度 |

Drain 需指定 `--ignore-daemonsets` 忽略 DaemonSet Pod。PDB 建议设置 `AlwaysAllow` 不健康 Pod 驱逐策略以避免阻塞。

### 集群升级

升级顺序：**etcd → kube-apiserver → kube-controller-manager → kube-scheduler → 节点 kubelet**。

kubeadm 集群使用 `kubeadm upgrade` 命令，手动部署按组件逐一升级。升级前必须备份 etcd。

### 监控体系：Pipeline

**资源指标管道**（轻量级）：cAdvisor → kubelet `/metrics/resource` → Metrics Server → `metrics.k8s.io` API → HPA/VPA/`kubectl top`

**完整指标管道**：Prometheus 抓取各组件 `/metrics` 端点 → 时序数据库 → Grafana 可视化 → `custom.metrics.k8s.io` API

### 排错方法论

系统化排错步骤：
1. `kubectl describe` — 查看资源状态事件
2. `kubectl logs` — 查看容器日志
3. `kubectl exec` — 进入容器调试
4. `crictl` — 检查 CRI 运行时状态
5. `kubectl debug` — 节点级调试
6. 检查 kubelet / 容器运行时服务状态
7. `journalctl` / systemd — 查看系统日志

## 常见问题与解决方案

| 问题 | 原因 | 方案 | 官方链接 |
|------|------|------|---------|
| etcd 成员故障导致集群不可用 | 多数 etcd 成员不可用，失去法定人数 | `etcdctl snapshot save` → `etcdutl snapshot restore` → 替换故障成员 → 重启组件 | [链接](https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/configure-upgrade-etcd/#restoring-an-etcd-cluster) |
| CoreDNS 无法解析 Service 名称 | Pod 未运行 / 权限不足 / 配置转发环 / Alpine musl DNS 问题 | 检查 Pod、Service、日志、`system:coredns` RBAC 权限、Corefile 配置 | [链接](https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/dns-debugging-resolution/) |
| kubectl drain 卡住无法驱逐 Pod | PDB 拒绝 / DaemonSet / nodeName 直接绑定 | `--ignore-daemonsets` / `--force` / `--delete-emptydir-data` / 配置 PDB `AlwaysAllow` | [链接](https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/safely-drain-node/) |
| etcd 磁盘空间不足（存储配额超限） | 默认 2GB 配额，频繁状态更新 | `etcdctl defrag` 碎片整理 / 配置 `auto-compaction-retention` / 提高 `--quota-backend-bytes`（推荐 8GB） | [链接](https://etcd.io/docs/latest/op-guide/maintenance/) |
| 节点 NotReady（kubelet 不可用） | kubelet 崩溃 / 容器运行时故障 / 节点磁盘压力 | `systemctl status kubelet` / `journalctl -xeu kubelet` / `crictl pods` / 重启 kubelet / `kubectl node-debug` | [链接](https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/crictl/) |
| Prometheus/Grafana 未显示节点数据 | 发现配置缺失 / RBAC 不足 / 端口未暴露 / node_exporter 未部署 | 验证 Target 状态 / 检查 RBAC / 测试 `/metrics` 端点 / 使用 Prometheus Operator 部署 | [链接](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config) |

## 官方最佳实践

### 1. etcd 定期备份策略

至少每天对 etcd 进行一次自动快照备份，加密存储到远程位置（S3 / MinIO）。备份在集群低峰期执行，保留最近 7 天备份。升级集群前必须手动创建额外备份。

参考：[K8s 官方文档](https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/configure-upgrade-etcd/)

### 2. 节点排水前配置 PDB

为关键应用配置 PodDisruptionBudget，设置 `Unhealthy Pod Eviction Policy` 为 `AlwaysAllow`，避免不健康 Pod 阻塞排水操作。`minAvailable` 不低于 1 或 `maxUnavailable` 不高于 1。

参考：[Safely Drain Node](https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/safely-drain-node/)

### 3. Prometheus 监控体系搭建

推荐 Prometheus Operator（`kube-prometheus-stack`）一键部署：Prometheus（指标采集 + 告警）、Grafana（可视化）、AlertManager（告警路由）、node_exporter（节点指标）、kube-state-metrics（K8s 资源指标）。推荐 Grafana Dashboard ID：**315**（K8s 集群监控）、**1860**（Node Exporter Full）。

参考：[Prometheus 官方文档](https://prometheus.io/docs/introduction/overview/)

### 4. 升级前准备清单

1. 备份 etcd 快照
2. 查阅目标版本变更日志（Changelog）
3. 检查废弃 API（removed API）并迁移 manifest
4. 确保所有节点运行支持的容器运行时版本
5. 在测试环境预升级验证
6. 规划维护窗口
7. 准备回滚方案
8. 按升级顺序执行（etcd → control plane → nodes）

### 5. 集群排错最佳路径

1. 确定问题范围：单 Pod / 单节点 / 整个集群？
2. 从最上层开始：`kubectl get events --all-namespaces --sort-by='.lastTimestamp'`
3. 逐层递进：`describe` → `logs` → `exec` → `crictl`
4. 节点问题用 `crictl` 代替 docker：`crictl ps`、`crictl logs`、`crictl exec`
5. 复杂问题用 `kubectl debug` 创建临时调试容器
6. 常见模式：`ImagePullBackOff`（检查镜像名/仓库权限）、`CrashLoopBackOff`（检查启动命令和资源限制）、`Pending`（检查资源不足或 PVC 未就绪）

参考：[Debug Cluster](https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/)

## 排查命令速查

```bash
# 集群状态
kubectl get events --all-namespaces --sort-by='.lastTimestamp'
kubectl describe node <node-name>

# etcd 操作
ETCDCTL_API=3 etcdctl snapshot save snapshot.db
etcdctl --endpoints=$ENDPOINT endpoint status --write-out=table
etcdctl --endpoints=$ENDPOINT defrag

# 节点维护
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
kubectl cordon <node>
kubectl uncordon <node>

# 运行时调试
crictl ps -a
crictl logs <container-id>
crictl exec -it <container-id> sh

# 监控
kubectl top node
kubectl top pod
kubectl port-forward svc/prometheus-grafana 3000:80

# 组件检查
systemctl status kubelet
journalctl -xeu kubelet
curl -k https://<node-ip>:10250/metrics
```

## 相关笔记

- [[etcd-集群运维]]
- [[k8s-monitoring-setup]]
- [[k8s-node-maintenance]]
