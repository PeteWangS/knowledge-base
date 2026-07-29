---
created: 2026-07-28
tags: [k8s, pod, deployment, service, kubernetes, core-concepts]
---

# K8s 核心概念：Pod、Deployment、Service 基础架构与设计原理

> **研究日期**：2026-07-28 | **优先级**：🔴最高 | **知识分类**：Kubernetes 核心

## 概述

Kubernetes 是一个开源的容器编排平台，核心目标是通过声明式配置自动管理容器的部署、伸缩和运行。**Pod**、**Deployment**、**Service** 是 Kubernetes 中最核心的三大概念：

- **Pod** — 调度的最小单元（逻辑主机），封装一个或多个共享网络和存储的容器
- **Deployment** — 声明式管理 Pod 的生命周期，支持滚动更新、回滚、自动伸缩
- **Service** — 为动态变化的 Pod 提供稳定的网络访问入口和解耦能力

三者组成了一套完整的应用部署模型：**Deployment 管理 Pod → Pod 运行容器 → Service 暴露网络端点**。

## 架构总览

![[assets/k8s/diagram-k8s-architecture.svg|697]]

Kubernetes 采用主从架构（Master-Worker）：

| 组件                      | 角色     | 说明                               |
| ----------------------- | ------ | -------------------------------- |
| kube-apiserver          | API 入口 | 所有组件交互的唯一入口，RESTful API          |
| etcd                    | 分布式存储  | 集群状态的唯一真实来源（source of truth）     |
| kube-scheduler          | 调度器    | 根据资源需求/约束将 Pod 分配到合适的 Node       |
| kube-controller-manager | 控制器管理器 | 运行所有控制器（Deployment、ReplicaSet 等） |
| kubelet                 | 节点代理   | 每个 Node 上的核心代理，负责管理 Pod 生命周期     |
| kube-proxy              | 网络代理   | 维护节点上的网络规则，实现 Service 流量转发       |

## 核心概念详解

### Pod

**Pod** 是 Kubernetes 中最小可部署的计算单元，是一组共享网络和存储的容器集合。Pod 模拟了一个「逻辑主机」的概念。

**关键特性：**
- Pod 是原子调度单元，一个 Pod 通常运行一个主容器
- 多容器 Pod 共享同一个网络命名空间，通过 `localhost` 通信
- Pod 名称需符合 DNS 子域名规范
- 通过 `restartPolicy` 控制容器重启行为（`Always`/`OnFailure`/`Never`）
- Pod 本质上是临时性的，不直接创建，而是通过工作负载资源（如 Deployment）管理

> ✅ **最佳实践**：始终使用 Deployment 等控制器创建 Pod，不要直接创建裸 Pod。直接创建的 Pod 在节点故障时不会自动恢复。

### Deployment

**Deployment** 提供 Pod 和 ReplicaSet 的声明式更新。它定义期望状态，Deployment Controller 以可控速率将实际状态调整为期望状态。

**关键特性：**
- 声明式更新 — `spec.replicas` 定义副本数
- 滚动更新策略 — 默认 `25% maxUnavailable`、`25% maxSurge`
- 自动回滚不健康发布
- 回滚历史多版本管理（`kubectl rollout undo`）
- 暂停部署可批量修改后统一发布（pause/resume）
- 支持自动伸缩（HPA）和比例伸缩（Proportional Scaling）

![[assets/k8s/diagram-deployment-rollout.svg]]

### Service

**Service** 是 Kubernetes 中的网络抽象层，为一组动态变化的 Pod 提供稳定的网络端点。

**四种类型：**

| 类型 | 访问范围 | 适用场景 |
|------|---------|---------|
| ClusterIP（默认） | 集群内部 | 内部微服务通信 |
| NodePort | 外部通过节点 IP+端口 | 开发测试、本地调试 |
| LoadBalancer | 外部负载均衡器 | 云原生生产环境 |
| ExternalName | DNS CNAME 映射 | 引入外部服务 |

**关键特性：**
- 通过标签选择器匹配后端 Pod
- 使用 EndpointSlice（替代已废弃的 Endpoints API）跟踪后端 Pod
- 每个 Pod 获取独立 IP，但 Pod 重建后 IP 变化
- Service 不关心 Pod 细节，只通过标签选择器匹配

### Pod 生命周期

![[assets/k8s/diagram-pod-lifecycle.svg]]

Pod 经历 **Pending → Running → Succeeded/Failed** 的生命周期：

| Phase | 说明 |
|-------|------|
| Pending | 等待调度和拉取镜像 |
| Running | 绑定节点且容器已创建 |
| Succeeded | 所有容器正常退出 |
| Failed | 至少一个容器非零退出 |
| Unknown | 节点通信故障 |

> ⚠️ **CrashLoopBackOff** 不是 Phase，而是容器 Status 字段。出现在容器反复崩溃时，kubelet 使用指数退避（10s→20s→40s…最高5分钟）进行重启。

### 容器探针（Probes）

Kubernetes 提供三种探针对容器进行健康诊断：

| 探针类型            | 失败后果           | 适用场景                    |
| --------------- | -------------- | ----------------------- |
| startup probe   | 容器无法启动         | 启动慢的应用（initialDelay 友好） |
| liveness probe  | 重启容器           | 死锁检测、故障恢复               |
| readiness probe | 从 Service 移除流量 | 蓝绿发布、优雅关闭、流量控制          |

探针支持四种检查方式：**HTTP GET**、**TCP Socket**、**Exec 命令**、**gRPC**。

> ⚠️ startup probe 优先于 liveness/readiness 运行，适合启动慢的应用。避免探针过于频繁引发波动。

## 常见问题与排查

| 问题                   | 原因                                                | 解决方案                                                              |
| -------------------- | ------------------------------------------------- | ----------------------------------------------------------------- |
| CrashLoopBackOff     | 应用代码错误、配置错误、资源不足、探针过早                             | `kubectl logs` → `kubectl describe pod` → 检查配置 → exec 进入调试        |
| 滚动更新卡住               | ImagePullBackOff、readiness 失败、maxUnavailable 配置不当 | `kubectl rollout status` → `describe deployment` → `rollout undo` |
| Service 后端无法访问       | 标签选择器不匹配、targetPort 错误、NetworkPolicy 阻止           | `describe service` 检查 Endpoints → 确认 selector → `port-forward` 测试 |
| ImagePullBackOff     | 镜像标签错误、私有仓库认证失败、镜像限流                              | `describe pod` 查看原因 → 配置 imagePullSecrets → 使用镜像加速器               |
| Pod Pending 无法调度     | 节点资源不足、taint/toleration、PVC 未绑定                   | `describe pod` 查看 Events → `describe node` 检查资源 → 检查 PVC          |
| OOMKilled / Eviction | 超过内存 limit、节点资源压力                                 | 合理设置 request/limit → 设置 PriorityClass → 配置 PDB                    |

## 最佳实践

### 1. 不要直接创建 Pod，始终使用工作负载资源

Deployment、StatefulSet、DaemonSet、Job 等提供控制器自动修复、滚动更新、回滚、伸缩能力。直接创建的 Pod 在节点故障时不会自动恢复。

### 2. 设置容器健康探针（startup + liveness + readiness）

- startup probe 防止启动慢的应用被误杀
- liveness probe 检测死锁并自动重启
- readiness probe 控制流量切入时机
- initialDelaySeconds 根据应用启动时间合理设置

### 3. 合理设置资源请求（requests）和限制（limits）

- requests 影响调度决策
- limits 防止资源争抢
- 内存 limits 超过时触发 OOM kill
- 使用 VPA 辅助推荐资源值

### 4. 使用命名空间隔离环境和资源配额

- 区分 dev/staging/prod 环境
- 结合 ResourceQuota 和 LimitRange
- Namespace 级别 NetworkPolicy 提供网络隔离

### 5. 使用 Service 实现前端后端解耦

- 必须使用 Service，不要直接引用 Pod IP
- DNS 发现：`<service>.<namespace>.svc.cluster.local`
- Ingress 统一管理 HTTP 路由规则
- Gateway API 提供更高级的流量管理

### 6. 设置 Pod 安全上下文（securityContext）

- 非 root 运行（runAsUser/runAsGroup）
- 只读根文件系统（readOnlyRootFilesystem: true）
- 遵循 Pod Security Standards（PSS）Baseline 策略

### 7. 精细调优滚动更新策略

- 生产环境配置 maxSurge 和 maxUnavailable
- 有状态服务：maxUnavailable=0
- 配合 PodDisruptionBudget（PDB）保证最低可用性
- 使用 pause/resume 批量修改后统一触发更新

## 排查命令速查

```bash
# Pod 日志
kubectl logs <pod-name>
kubectl logs -f <pod-name>                        # 实时追踪

# Pod 详情
kubectl describe pod <pod-name>

# 进入 Pod 调试
kubectl exec -it <pod-name> -- /bin/sh
kubectl exec -it <pod-name> -- /bin/bash

# Deployment 操作
kubectl rollout status deployment/<name>
kubectl rollout undo deployment/<name>
kubectl rollout undo deployment/<name> --to-revision=N
kubectl rollout pause deployment/<name>
kubectl rollout resume deployment/<name>
kubectl rollout history deployment/<name>

# Service 调试
kubectl describe service <name>
kubectl get endpoints
kubectl port-forward pod/<pod-name> 8080:80       # 直接连接 Pod 测试

# 节点信息
kubectl get nodes
kubectl describe node <node-name>

# 事件
kubectl get events --sort-by='.lastTimestamp'

# 常用组合（JSONPath）
kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\n"}{end}'
```

## 官方参考文档

- [Pods - Kubernetes 官方文档](https://kubernetes.io/docs/concepts/workloads/pods/)
- [Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
- [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [Service](https://kubernetes.io/docs/concepts/services-networking/service/)
- [Liveness, Readiness, and Startup Probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)
- [Resource Management for Pods and Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
- [Node-pressure Eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/)

## 相关笔记

- [[k8s-ops-essentials]] — K8s 运维基础与集群管理
- [[k8s-etcd-backup-restore]] — etcd 备份与恢复
- [[k8s-monitoring-pipeline]] — K8s 监控流水线
