---
created: 2026-08-23
topic: K8s实战
subtopic: 集群排错工具链深潜
tags: [kubernetes, kubectl-debug, crictl, audit-log, logging]
---

# K8s 集群排错工具链深潜：kubectl debug / crictl / 审计 / 日志

## 概述

「集群排错」这一 K8s 实战子主题此前已产出基础方法论、官方排错全景、Pod 状态机/etcd/Prometheus 深潜等笔记。本轮取全新角度：**排错工具链与可观测性深潜**——kubectl debug 的三种姿势（临时容器/复制 Pod/节点 shell）与六档调试 Profile、crictl 在节点上绕过 kubectl 直查 CRI 运行时、kube-apiserver 审计日志（四级策略/双后端/批处理调优）、以及集群级日志的三种架构（节点代理/边车/应用直推）与 klog 系统日志体系（结构化/上下文/JSON 格式、v1.36 稳定的 Log Query 远程查节点日志）。

这些内容补全了排错工具箱中「进容器之后、看日志之时」的缺失一环：`kubectl exec` 失败（镜像无 shell）之后的出路、kubectl 不可用（kubelet 挂掉/API server 不可达）时节点上的运行时真相源、以及安全审计与日志体系的官方配置姿势。

## 架构图

![[assets/k8s/diagram-k8s-debug-toolchain-arch.svg]]

*图：排错工具链四层视角——集群/节点/容器/日志，运维人员按需逐级深入*

排错工具链按「集群视角 → 节点视角 → 容器视角 → 日志视角」四层组织：

- **集群视角**：`kubectl describe/logs/exec` 走 API server 授权链，适用于多数场景；
- **节点视角**：`kubectl debug node` 在目标节点起调试 Pod（根文件系统挂载到 /host），或 `crictl` 直连 CRI socket（`unix:///var/run/containerd/containerd.sock`）绕过 API server 查运行时真相；
- **容器视角**：`kubectl debug` 临时容器（ephemeral container）通过专用 ephemeralcontainers API handler 注入运行中 Pod（Pod spec 不可变，故不能 `kubectl edit` 添加），或 `--copy-to` 复制 Pod 改镜像/命令/加容器；
- **日志视角**：容器日志由 kubelet 按 CRI 日志格式收进 /var/log/pods 并轮转（containerLogMaxSize 默认 10Mi、containerLogMaxFiles 默认 5），系统组件日志走 journald（Linux systemd 下 `journalctl -u kubelet`）或 /var/log 文件，审计日志由 kube-apiserver 按 Policy 分级（None/Metadata/Request/RequestResponse）写入 JSONlines 文件或 webhook 后端。

> 安全红线：`nodes/proxy` 权限可执行节点上任意容器命令；Log Query 的 `enableSystemLogHandler` 默认关闭；调试完即关。

## 核心概念

### 临时容器 Ephemeral Containers（v1.25 起 stable）

一种特殊容器，临时运行在已存在的 Pod 内，用于用户发起的排错动作，如检查服务状态、运行任意命令。由于 Pod 创建后不可再添加容器（Pod 不可变设计），临时容器通过 API 中专门的 ephemeralcontainers handler 注入，而非写进 pod.spec——因此**不能用 `kubectl edit` 添加**。

**关键点**：无资源/执行保证、永远不会被自动重启，不适合构建应用；ContainerSpec 中 ports/livenessProbe/readinessProbe/resources 等字段被禁用（Pod 资源分配不可变）；静态 Pod 不支持临时容器。

### kubectl debug 三种姿势

| 姿势 | 命令 | 适用场景 |
|------|------|---------|
| ① 临时容器 | `kubectl debug -it <pod> --image=busybox:1.28 --target=<container>` | 向运行中 Pod 注入调试容器并自动 attach（--target 指定进程命名空间目标，需运行时支持） |
| ② 复制 Pod | `kubectl debug <pod> --copy-to=<name>` | 派生副本，可 `--set-image=*=ubuntu` 换镜像、`--container=myapp -- sh` 改命令、`--share-processes` 共享进程命名空间 |
| ③ 节点 shell | `kubectl debug node/<node> -it --image=ubuntu` | 在目标节点起调试 Pod，根文件系统挂载在 /host |

**关键点**：`-i` 默认自动 attach，可用 `--attach=false` 取消、断线后用 `kubectl attach` 重连；调试 Pod 用完必须删除（`kubectl delete pod <name> --now`）；节点 shell 场景中 Pod 运行在 host IPC/Network/PID 命名空间但非特权，`chroot /host` 会失败，需要特权时加 `--profile=sysadmin`。

排错阶梯（官方推荐路径）：

![[assets/k8s/diagram-k8s-debug-ladder-flow.svg]]

*图：排错阶梯——describe → logs → exec → debug 临时容器 → copy-to → 节点 shell，crictl 作为 kubelet 不可用时的兜底*

### 调试 Profile（--profile）

`kubectl debug` 在创建调试 Pod/临时容器/复制 Pod 时可应用预置安全属性集合：

| Profile | 含义 |
|---------|------|
| legacy | 默认，向后兼容 1.22，**计划弃用** |
| general | 通用合理属性，**推荐** |
| baseline / restricted | 对齐 Pod Security Standard 两级策略 |
| netadmin | 网络管理员权限（抓包/网络诊断） |
| sysadmin | root 级系统管理员权限 |

v1.32 起支持 `--custom=<yaml>` 自定义 Profile（只允许改容器 spec 的 env/securityContext 等，不允许改 name/image/command/lifecycle/volumeDevices 及 Pod spec）。

**关键点**：sysadmin Profile 使容器进程获得全部 capabilities（CapEff=000001ffffffffff，securityContext.privileged=true），用于 tcpdump 抓包等场景；默认 legacy 即将弃用，应显式指定 general。

### crictl（CRI 兼容运行时 CLI）

crictl 是 CRI 兼容容器运行时的命令行接口（托管于 kubernetes-sigs/cri-tools），在节点上**直接与运行时通信**，kubelet 不可用/API server 不可达时仍能查看真实运行时状态。端点配置三选一：

- `--runtime-endpoint` / `--image-endpoint` 标志
- `CONTAINER_RUNTIME_ENDPOINT` / `IMAGE_SERVICE_ENDPOINT` 环境变量
- `/etc/crictl.yaml` 配置文件（containerd 例：`runtime-endpoint: unix:///var/run/containerd/containerd.sock`）

**关键点**：核心命令 `crictl pods`（--name/--label 过滤）、`crictl images`（-q 只出 ID）、`crictl ps -a`（**ATTEMPT 列即重启次数**）、`crictl exec -i -t <id> <cmd>`、`crictl logs --tail=N <id>`；不配置端点时会探测已知端点列表，影响性能。

### 审计日志 Auditing（kube-apiserver）

审计提供与安全相关的、按时间排序的集群动作记录，回答 what/when/who/on what/where/from where/to where 七问。每个请求按阶段（RequestReceived/ResponseStarted/ResponseComplete/Panic）生成审计事件，先按 Policy 规则顺序匹配出审计级别（**None/Metadata/Request/RequestResponse**），再写入后端（Log 文件 JSONlines 格式 / Webhook 外部 HTTP API）。

**关键点**：未传 `--audit-policy-file` 则完全不记审计；rules 字段必须提供（0 条规则视为非法）；patch 请求的 request body 是 JSON 数组（patch 操作列表）而非对象；审计会增大 API server 内存占用（每请求保存上下文）。

### 审计后端与批处理调优

- **Log 后端**标志：`--audit-log-path` / `-maxage` / `-maxbackup` / `-maxsize`（轮转保留）
- **Webhook 后端**：`--audit-webhook-config-file`（kubeconfig 格式）、`--audit-webhook-initial-backoff`
- 两者都支持 **batch / blocking / blocking-strict** 三种缓冲策略：webhook 默认 batch（缓冲 10000 事件、每批 400、最长等 30s、节流 10 qps/突发 15），log 默认 blocking（逐事件阻塞）。blocking-strict 在 RequestReceived 阶段审计失败时**整个 API 请求失败**

**关键点**：调参公式示例——100 req/s 只审计两阶段≈200 事件/s，批 100 事件则节流≥2 qps，后端写 5 秒延迟则 buffer≥1000 事件；默认参数多数场景够用，用 Prometheus 指标 `apiserver_audit_event_total` / `apiserver_audit_error_total` 监控丢事件。**截断默认关闭，管理员应开启 `--audit-log-truncate-enabled`**。

### 集群级日志三种架构

Kubernetes 无原生集群级日志方案，官方给出三种架构：

| 架构 | 原理 | 优缺点 |
|------|------|--------|
| ① 节点级日志代理 | DaemonSet 每节点一个，收集 /var/log/pods 下容器日志转发到后端 | 应用零改造、每节点仅一个，**推荐主力** |
| ② 应用内边车 | streaming 边车把日志重写到自身 stdout 走 kubelet 通道；或 fluentd 边车直接采集文件 | streaming 写文件再流到 stdout 使节点存储需求**翻倍**；fluentd 边车资源消耗大、日志不受 kubelet 管理、`kubectl logs` 不可见 |
| ③ 应用直推 | 应用直接把日志推到后端 | 超出 Kubernetes 范围 |

![[assets/k8s/diagram-k8s-cluster-logging-arch.svg]]

*图：集群级日志三种架构对比——节点代理（主力）、应用内边车、应用直推*

**关键点**：官方建议单文件应用直接写 /dev/stdout 走 kubelet 通道，避免 streaming 边车翻倍存储。

### klog 系统日志体系与 Log Query

klog 是 K8s 组件日志库，v1.26 起旧标志（--logtostderr/--log-file 等 11 个）已移除，**输出恒定到 stderr**，由调用方（systemd/POSIX shell）重定向，distroless 环境用 kube-log-runner 包装重定向。日志演进三阶段：

1. 传统 klog 原生格式
2. v1.23 结构化日志（beta，文本格式向后兼容）
3. v1.30 上下文日志（WithValues/WithName 注入调用链信息）

`--logging-format=json` 输出 JSON（ts/v/err/msg 键，4 个核心组件支持）。**v1.36 起 NodeLogQuery 稳定**：`kubectl get --raw '/api/v1/nodes/<node>/proxy/logs/?query=kubelet'` 远程查节点服务日志。

**关键点**：Log Query 需要 kubelet 配置 `enableSystemLogHandler=true`（默认 false，建议仅调试时开启）与 `enableSystemLogQuery=true`；查询选项 query（必填）/pattern（PCRE）/sinceTime/untilTime/tailLines/boot；注意：nodes/proxy 的 get 权限即可通过 kubelet API 在节点任意容器执行命令，授权务必谨慎。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|---------|---------|
| kubectl exec 报 `OCI runtime exec failed: exec: "sh": executable file not found in $PATH` | 容器镜像无 shell 或无调试工具（distroless 刻意不含 shell）；或容器已崩溃 | `kubectl debug -it <pod> --image=busybox:1.28 --target=<container>` 注入临时容器；崩溃时 `--copy-to` 复制 Pod 改命令/镜像；调试完删除 Pod | [Debug Running Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/#ephemeral-container) |
| 节点无法 SSH 登录，无法排查 kubelet/containerd | SSH 被禁用/网络不可达/无密钥，但节点仍在线 | `kubectl debug node/<node> -it --image=ubuntu` 建调试 Pod；根文件系统在 /host（看 /host/var/log/kubelet.log 等）；需特权加 `--profile=sysadmin`；用完 `kubectl delete pod node-debugger-<node>-<suffix> --now` | [kubectl-node-debug](https://kubernetes.io/docs/tasks/debug/debug-cluster/kubectl-node-debug/) |
| 节点宕机/不可达，kubectl debug node 报错 | debug node 本质是在目标节点调度调试 Pod，节点自身不可用时调度必失败 | 按 down/unreachable node 流程：从控制面确认节点状态、从健康节点/API server 视角收集信息，必要时重建节点；不要对宕机节点用 kubectl debug | [Debugging down/unreachable nodes](https://kubernetes.io/docs/tasks/debug/debug-cluster/#example-debugging-a-down-unreachable-node) |
| kubectl logs 只能看到少量日志 | kubelet 轮转：containerLogMaxSize 默认 10Mi、containerLogMaxFiles 默认 5；kubectl logs 只返回最新一个日志文件 | 临时：`kubectl logs --previous` 看崩溃前实例；永久：调大轮转配置或部署节点级日志代理汇聚长期留存 | [Log rotation](https://kubernetes.io/docs/concepts/cluster-administration/logging/#log-rotation) |
| 配置了 --audit-policy-file 但审计无事件 | 策略规则未匹配（按顺序首个匹配生效，全不匹配不记录）；或 rules 缺失（0 条视为非法）；或 --audit-log-path 未配置 | 最小策略验证：`rules: - level: Metadata`；同时配 --audit-policy-file 与 --audit-log-path；「越具体越靠前」排序；静态 Pod 确认 hostPath 卷挂载 | [Audit policy](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/#audit-policy) |
| 审计日志写满磁盘 / API server 变慢 | Log 后端默认 blocking 逐事件阻塞写盘，未配轮转与截断（truncate 默认关闭） | 配轮转三件套 --audit-log-maxsize/maxage/maxbackup；开 --audit-log-truncate-enabled；策略降级（多数 Metadata，敏感资源才 Request/RequestResponse）；用 apiserver_audit_event_total/error_total 监控 | [Parameter tuning](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/#parameter-tuning) |
| crictl 报 connection refused | 未配置端点，或 /etc/crictl.yaml 指向错误 socket（containerd 是 /var/run/containerd/containerd.sock，CRI-O 是 /var/run/crio/crio.sock） | 显式配置 runtime-endpoint 与 image-endpoint；对照 crictl ps -a 的 ATTEMPT 列（重启次数）与 kubectl describe 的 Restart Count | [crictl usage](https://kubernetes.io/docs/tasks/debug/debug-cluster/crictl/#general-usage) |
| 节点上找不到 kubelet 日志（/var/log/kubelet.log 不存在） | Linux systemd 下 kubelet/运行时默认写 journald 而非文件；v1.26 起 klog 输出恒定到 stderr | `journalctl -u kubelet`；要文件输出用 kube-log-runner 包装；远程查用 Log Query（需 enableSystemLogHandler=true） | [System logs](https://kubernetes.io/docs/concepts/cluster-administration/system-logs/#klog) |
| 组件日志挂给静态 Pod 后无限增长不轮转 | K8s 不管理组件日志轮转——依赖节点级机制（logrotate）；容器日志轮转由 kubelet 负责，组件日志不在其管辖 | 自行配 logrotate 覆盖组件日志目录；或让组件输出到 stdout/stderr 由 kubelet 统一轮转；确认部署工具（kubeadm 等）自带轮转归属 | [System component logs](https://kubernetes.io/docs/concepts/cluster-administration/logging/#system-component-logs) |
| Log Query 返回 403/404 | 功能未启用：enableSystemLogHandler 默认 false（v1.36 起 NodeLogQuery feature gate 已锁定 true，但 enableSystemLogHandler 仍须手动开）；或调用者缺 nodes/proxy 权限 | kubelet 配置设 enableSystemLogHandler: true（+ enableSystemLogQuery: true）重启，仅调试期开启；RBAC 授予最小 nodes/proxy get 权限——该权限可经 kubelet API 在节点任意容器执行命令，务必谨慎 | [Log query](https://kubernetes.io/docs/concepts/cluster-administration/system-logs/#log-query) |

## 最佳实践

1. **排错阶梯：从 describe 到节点 shell 逐级升级** — `kubectl describe pod` 看事件 → `kubectl logs/--previous` 看日志 → `kubectl exec` 进容器 → `kubectl debug` 临时容器（镜像无 shell/容器崩溃时）→ `--copy-to` 复制 Pod 改配置 → `kubectl debug node` 节点级 shell。每一步只做必要动作，避免一上来就用高权限调试会话。（[debug-running-pod](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/)）

2. **调试会话即用即删，Profile 按需选择** — kubectl debug 创建的临时容器/调试 Pod 用完立即 `kubectl delete --now`；不指定 --profile 时默认 legacy（计划弃用），生产显式指定 general；抓包/网络诊断用 netadmin，root 级操作才用 sysadmin（privileged:true）。调试节点前先确认节点在线，宕机节点走集群排错流程而非 kubectl debug node。（[debugging-profiles](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/#debugging-profiles)）

3. **应用日志直接写 stdout/stderr，轮转交给 kubelet** — 容器应用写 stdout/stderr 即可获得 kubelet 的 CRI 日志收集与轮转（containerLogMaxSize 默认 10Mi、containerLogMaxFiles 默认 5，可调），并天然兼容 kubectl logs。单文件日志场景直接写 /dev/stdout，避免 streaming 边车使节点存储需求翻倍。（[log-rotation](https://kubernetes.io/docs/concepts/cluster-administration/logging/#log-rotation)）

4. **集群级日志首选节点级代理（DaemonSet）** — 官方推荐主力方案：每节点一个日志代理 DaemonSet，收集 /var/log/pods 下所有容器日志转发到后端，应用零改造。边车方案按需用于多格式分流，但注意 fluentd 边车资源消耗大且日志不受 kubelet 管理（kubectl logs 不可见）。（[logging-architectures](https://kubernetes.io/docs/concepts/cluster-administration/logging/#cluster-level-logging-architectures)）

5. **审计策略分级配置：敏感资源详记、全局兜底 Metadata** — 策略按规则顺序首个匹配生效，把高敏感资源（secrets、serviceaccounts token、RBAC 变更等）的 Request/RequestResponse 规则写在前面，末尾用 level: Metadata 兜底全量请求；无审计需求时不要配置 --audit-policy-file（不配置即不产生事件与开销）。开启后必须配套轮转（maxsize/maxage/maxbackup）与截断（truncate-enabled）防磁盘写满。（[audit-policy](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/#audit-policy)）

6. **监控审计子系统健康，防静默丢事件** — 审计后端（尤其 webhook batch 模式）缓冲区溢出会丢事件：用 Prometheus 指标 apiserver_audit_event_total 与 apiserver_audit_error_total 跟踪；按流量调参（batch-buffer-size/max-size/max-wait/throttle-qps）；高可用审计链路用 webhook 双后端 + blocking-strict 保证 RequestReceived 阶段失败即拒请求。（[parameter-tuning](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/#parameter-tuning)）

7. **系统日志走结构化 + JSON，远程查询用 Log Query** — 新部署集群利用 v1.23+ 结构化日志与 v1.30 上下文日志（WithValues 自动带调用链信息），程序化解析用 --logging-format=json（注意处理非 JSON 行与启动期日志）；远程排查节点服务日志用 v1.36 稳定的 Log Query，但 enableSystemLogHandler 仅调试期开启，nodes/proxy 权限最小化授予。（[system-logs](https://kubernetes.io/docs/concepts/cluster-administration/system-logs/)）

8. **crictl 作为 kubectl 不可用时的运行时真相源** — kubelet 挂掉或 API server 不可达时，crictl 是节点上最后的运行时观察手段：crictl ps -a 看容器与 ATTEMPT（重启次数）、crictl logs 看原始日志、crictl exec 进容器、crictl images 查镜像。生产节点预装与 K8s 版本匹配的 crictl 并写好 /etc/crictl.yaml（containerd socket），避免故障时手忙脚乱。（[crictl](https://kubernetes.io/docs/tasks/debug/debug-cluster/crictl/)）

## 排查命令

```bash
# --- kubectl debug 三姿势 ---
# ① 临时容器（镜像无 shell / 容器崩溃时）
kubectl debug -it <pod> --image=busybox:1.28 --target=<container>
# ② 复制 Pod（换镜像/改命令）
kubectl debug myapp -it --copy-to=myapp-debug --container=myapp -- sh
kubectl debug <pod> --copy-to=<name> --set-image=*=ubuntu --share-processes
# ③ 节点 shell（根文件系统在 /host）
kubectl debug node/<node> -it --image=ubuntu --profile=sysadmin
# 调试完即删
kubectl delete pod <debug-pod-name> --now

# --- crictl（节点上直查 CRI 运行时）---
crictl ps -a                    # ATTEMPT 列 = 容器重启次数
crictl logs --tail=50 <id>      # 原始容器日志
crictl exec -i -t <id> sh       # 进容器
crictl images -q                # 镜像 ID 列表
# /etc/crictl.yaml: runtime-endpoint: unix:///var/run/containerd/containerd.sock

# --- 审计日志（kube-apiserver 标志）---
--audit-policy-file=/etc/kubernetes/audit-policy.yaml
--audit-log-path=/var/log/kubernetes/audit/audit.log
--audit-log-maxsize=100 --audit-log-maxage=7 --audit-log-maxbackup=5
--audit-log-truncate-enabled
# 监控：apiserver_audit_event_total / apiserver_audit_error_total

# --- 系统日志 ---
journalctl -u kubelet           # systemd 下 kubelet 日志
journalctl -u containerd        # 容器运行时日志
# Log Query（v1.36 稳定，需 enableSystemLogHandler=true）
kubectl get --raw '/api/v1/nodes/<node>/proxy/logs/?query=kubelet'
kubectl get --raw '/api/v1/nodes/<node>/proxy/logs/?query=containerd&pattern=error&tailLines=100'
```

## 相关笔记

- [[k8s-cluster-troubleshooting]] — 集群排错全景方法论（本笔记聚焦工具链与可观测性，互为补充）
- [[k8s-cluster-ops-deepdive]] — 官方排错全景与运维深潜
- [[k8s-cluster-ops-combat]] — 集群运维实战基础方法论
- [[k8s-pod-deployment-service]] — Pod 生命周期与工作负载管理

## 官方参考

- [Ephemeral Containers（概念）](https://kubernetes.io/docs/concepts/workloads/pods/ephemeral-containers/)
- [Debug Running Pods（kubectl debug 三种姿势与 Profile）](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/)
- [Get a Shell to a Running Container](https://kubernetes.io/docs/tasks/debug/debug-application/get-shell-running-container/)
- [Debugging Kubernetes Nodes With Kubectl](https://kubernetes.io/docs/tasks/debug/debug-cluster/kubectl-node-debug/)
- [Debugging Kubernetes nodes with crictl](https://kubernetes.io/docs/tasks/debug/debug-cluster/crictl/)
- [Auditing（审计日志：策略/后端/批处理调优）](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/)
- [Logging Architecture（容器日志/轮转/集群级日志三架构）](https://kubernetes.io/docs/concepts/cluster-administration/logging/)
- [System Logs（klog/结构化/上下文/JSON/Log Query）](https://kubernetes.io/docs/concepts/cluster-administration/system-logs/)
- [cri-tools（crictl 源码与文档）](https://github.com/kubernetes-sigs/cri-tools)
