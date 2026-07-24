---
created: 2026-07-16
source: kubernetes.io/docs, github.com/kubernetes/website
topic: Kubernetes
priority: 🔴 最高
---

# Kubernetes — Pod 基础概念与架构

## 概述

**Pod** 是 Kubernetes 中最小的可部署计算单元，是一个或多个容器的组合，共享同一份网络命名空间（IP + 端口）和存储卷。Pod 相当于"逻辑主机"，其内容总是被协同调度到同一节点上。

> 📌 官方定义：_Pods are the smallest deployable units of computing that you can create and manage in Kubernetes._

---

## 架构图

![[assets/k8s/pod-architecture.svg|697]]

### 核心组件说明

| 组件 | 作用 | 与其它组件关系 |
|------|------|-------------|
| **Pause 容器** | 持有 Pod 的网络命名空间 | 同一 Pod 内所有容器共享其 IP |
| **业务容器** | 运行实际应用（nginx、app 等） | 通过 localhost 通信，共享 Volume |
| **emptyDir Volume** | Pod 级别的临时存储 | 容器间共享，Pod 删除即销毁 |
| **kubelet** | 节点代理，管理 Pod 生命周期 | 与 API Server 通信，对接容器运行时 |
| **Container Runtime** | 负责拉取和运行容器（containerd） | 受 kubelet 调用 |

---

## 核心概念

### 1. Pod 是调度单位，不是容器

Kubernetes 不直接调度容器，而是调度 Pod。即便只有一个容器，它也运行在一个 Pod 内。

- **一句记住**：Pod = 容器 + 共享环境（网络 + 存储）
- **创建方式**：一般不直接创建，通过 Deployment / StatefulSet / Job 等 Workload 资源

### 2. 单容器 Pod vs 多容器 Pod

| 类型 | 场景 | 示例 |
|------|------|------|
| **单容器 Pod** | 最常用模式，Pod 只包装一个容器 | nginx Pod |
| **多容器 Pod** | 紧耦合容器共享资源 | Sidecar 日志收集 + 主应用 |

> 多容器模式仅用于**紧耦合**场景，不要为了高可用把多个相同容器放一个 Pod 里（应使用 Deployment + Replicas）

### 3. Pod 生命周期（Phase）

![[assets/k8s/pod-lifecycle-phase.svg]]

| Pod Phase | 含义 | 常见原因 |
|-----------|------|---------|
| **Pending** | Pod 已被集群接受，容器未就绪 | 镜像拉取中、资源不足等待调度 |
| **Running** | Pod 已绑定到节点，至少一个容器在运行 | — |
| **Succeeded** | 所有容器成功退出（Job 类 Pod） | 批处理任务完成 |
| **Failed** | 容器异常退出 | 应用崩溃、OOM |
| **Unknown** | 无法获取 Pod 状态 | Node 失联、kubelet 停止 |

### 4. 容器状态

![[assets/k8s/container-state-flow.svg]]

每个容器的细化状态：
- **Waiting**：启动中（拉镜像、挂载 Secret）
- **Running**：正常运行
- **Terminated**：已退出（成功或失败）

> 排错时 `kubectl describe pod` 可看到每个容器的状态和原因

---

## 常见问题与解决方案

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|---------|---------|
| **Pod 卡在 Pending** | 资源不足、无匹配 nodeSelector、PVC 未就绪 | `kubectl describe pod` 看 Events → 调整资源/节点选择 | [Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) |
| **CrashLoopBackOff** | 应用启动失败、配置错误 | `kubectl logs <pod>` 查看错误日志 | [Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) |
| **ImagePullBackOff** | 镜像名错误、私有仓库未认证 | 检查镜像拼写、配置 imagePullSecrets | [Images](https://kubernetes.io/docs/concepts/containers/images/) |
| **OOMKilled** | 容器内存超限 | 调大 `resources.limits.memory` 或优化内存 | [Resource Limits](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) |
| **Node 失联 → Unknown** | 节点宕机、kubelet 停止 | 恢复节点，Controller 自动重建 Pod | [Node Controller](https://kubernetes.io/docs/concepts/architecture/nodes/) |
| **Terminating 卡住** | 容器忽略 SIGTERM、finalizer 未清理 | `kubectl delete pod --force --grace-period=0` | [Force Delete](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination-forced) |

---

## 官方最佳实践

来源：https://github.com/kubernetes/website

### 1. 不要直接创建 Pod

始终通过 Deployment / StatefulSet / DaemonSet 等控制器创建，让控制器处理滚动更新、扩容、自愈。

```yaml
# ✅ 正确做法：用 Deployment 管理 Pod
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-deployment
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:1.14.2
        ports:
        - containerPort: 80
```

### 2. 设置资源 requests 和 limits

- **requests**：调度保证量，集群按此值做容量规划
- **limits**：运行硬上限，超限触发 OOMKill 或 CPU 限流
- 黄金法则：`requests.memory ≤ limits.memory`，生产环境两者都要设

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

### 3. 配置健康检查探针

| 探针 | 作用 | 失败后果 |
|------|------|---------|
| **livenessProbe** | 容器是否存活 | 重启容器 |
| **readinessProbe** | 容器是否就绪 | 从 Service Endpoints 移除 |
| **startupProbe** | 容器是否已启动 | 启动阶段屏蔽 liveness/readiness |

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
```

### 4. 正确设置 Pod OS 字段（v1.25+）

```yaml
spec:
  os:
    name: linux  # 或 windows
```

kubelet 会拒绝与节点 OS 不匹配的 Pod。

---

## 排查命令速查

```bash
# 查看 Pod 状态
kubectl get pods -o wide
kubectl describe pod <name>        # Events 是排错关键线索

# 查看日志
kubectl logs <name>                # 当前容器日志
kubectl logs <name> --previous     # 上次启动的日志（CrashLoop 用）
kubectl logs -f <name>             # 实时跟踪

# 进入 Pod 调试
kubectl exec -it <name> -- /bin/sh
kubectl exec -it <name> -c <容器名> -- /bin/sh  # 多容器时指定

# 查看完整 YAML
kubectl get pod <name> -o yaml

# 资源监控
kubectl top pod
kubectl top node
```

---

## 相关笔记

- [[Kubernetes Deployment 详解]]
- [[Docker 容器基础]]
- [[K8s Service 网络模型]]
- [[K8s 存储之 PV 与 PVC]]

---

> 📚 来源：[Kubernetes 官方文档 - Pods](https://kubernetes.io/docs/concepts/workloads/pods/) | [kubernetes/website](https://github.com/kubernetes/website)
