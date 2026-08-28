---
created: 2026-08-28
tags: [k8s, network, tcpip, conntrack, overlay, kube-proxy]
---

# Kubernetes 网络数据面与 TCP/IP 原理：数据包流转路径 / MTU / conntrack / 源IP / overlay

## 概述

TCP/IP 是 Kubernetes 网络模型的底层协议栈。K8s 网络模型的核心设计是「每个 Pod 拥有一个真实（非机器私有）IP 地址」，形成**无 NAT 的扁平地址空间**：Pod 间通信无需代理或地址翻译，应用看到的就是对端可见的源 IP，自注册与服务发现机制开箱即用（官方设计文档原话：NAT-less, flat address space）。在此之上，Service 抽象创建虚拟 IP（VIP），由各节点上的 kube-proxy 编程 iptables/nftables/IPVS 规则，对 TCP/UDP 流做 DNAT/SNAT 转换实现透明负载均衡。

因此理解 K8s 网络排障，本质是理解**数据包在集群中的完整流转路径**：三次握手如何穿越 veth 对、Linux bridge、iptables DNAT 与 conntrack 状态跟踪；overlay 网络（VXLAN 等）如何封装内层包并影响 MTU；externalTrafficPolicy 如何决定源 IP 是否被改写。

本轮笔记以「数据面」视角展开：先讲 TCP/IP 落地的四类网络问题与三段 IP 规划，再逐条走通 Pod→Pod、Pod→Service、外部→Service 三条数据包路径，最后落到 conntrack、流量策略与官方推荐的最佳实践。与 [[k8s-network-dns-lb-cni]]（控制面与规则视角）和 [[k8s-cluster-networking]]（全景视角）互补。

## 架构图

![[assets/network/diagram-dataplane-arch.svg]]

*图：K8s 网络数据面四层架构——容器层（共享 netns）→ Pod 网络层（bridge/overlay）→ Service 层（kube-proxy DNAT + conntrack）→ 外部入口层（LB/NodePort）。*

数据面架构自上而下分四层：

1. **容器层**：同一 Pod 内容器共享网络命名空间，经 localhost 通信（对应四类网络问题之一，由 Pod 与 localhost 解决）；
2. **Pod 网络层**：每个 Pod 由 CNI 插件分配真实 IP（网络插件配置 Pod CIDR），同节点 Pod 经 veth pair 挂到 Linux bridge（如 cbr0），跨节点 Pod 经节点路由（每节点一段 Pod 子网）或 overlay 封装（VXLAN/GRE）互通，**整网无 NAT**；
3. **Service 层**：kube-apiserver 从 Service CIDR 分配 VIP，kube-proxy 在每个节点安装 iptables/nftables/IPVS 规则，将访问 VIP 的 TCP/UDP 流 DNAT 到后端 Pod，依赖 conntrack 做回程逆向转换；
4. **外部入口层**：LoadBalancer/NodePort 将外部流量送进节点，经规则识别 Service 后转发到后端 Pod（外部 LB 场景存在双跳：LB→节点→后端 Pod），NodePort/LB 场景下客户端源 IP 会被改写（SNAT），`externalTrafficPolicy: Local` 可保留源 IP。

三段 IP 规划（Pod/Service/Node CIDR）互不重叠，分别由网络插件、kube-apiserver、kubelet/cloud-controller-manager 分配。

## 核心概念

### 1. 四类网络问题与 NAT-less 扁平地址空间

K8s 网络要解决 4 个问题：容器间通信（Pod 内 localhost）、Pod 间通信、Pod 到 Service、外部到 Service。核心设计是每个 Pod 获得真实 IP（非机器私有地址），Pod 间通信不需要代理或翻译，容器内 `SIOCGIFADDR` 看到的 IP 与对端看到的源 IP 一致。

> 要点：与 Docker 172-dot 私有 IP 模型相反——Docker 容器无法被对端以自身知晓的 IP 访问，K8s 的扁平地址空间让自注册、IP 分发机制直接可用（官方设计文档明示 NAT-less, flat address space）。

### 2. 三段 IP 地址规划（Pod/Service/Node CIDR）

集群要求 Pod、Service、Node 三段 IP **互不重叠**：网络插件为 Pod 分配 IP，kube-apiserver 为 Service 分配 VIP，kubelet 或 cloud-controller-manager 为 Node 分配 IP。按 IP 族可分为 IPv4-only、IPv6-only、dual-stack，dual-stack 要求所有组件主 IP 族一致。

> 要点：集群类型只看 Pod/Service/Node 对象上的 IP（`pod.status.ips` / `node.status.addresses`），与主机其他网卡 IP 无关；dual-stack 下所有组件必须协商一致的主 IP 族。

### 3. 数据包路径一：Pod→Pod 直连

同节点 Pod：经 veth pair 进入 Linux bridge（cbr0），二层直通；跨节点 Pod：每节点拥有独立 Pod 子网段，通过节点路由（GCE 高级路由/ip-forwarding）或 overlay 网络（VXLAN/GRE）送达，**全程无地址翻译**。

> 要点：官方实现示例——GCE 用 advanced routing rules 为每台 VM 路由一个额外 /24 Pod 网段，目标节点收到后经 cbr0 桥转发给目标 Pod。

### 4. 数据包路径二：Pod→Service（kube-proxy DNAT）

客户端连接 Service VIP 时，iptables 规则链逐级匹配：先按 Service 的规则，再按 endpoint 规则，最终以目标地址 NAT（DNAT）把包重定向到后端 Pod，默认随机选择后端（iptables 模式），**不重写客户端源 IP**。

> 要点：回程包依赖 conntrack 状态表逆向转换（把后端 Pod 的响应 DNAT 回 VIP），因此 **DNAT 与 conntrack 是 Service 数据面的核心耦合点**。

### 5. 数据包路径三：外部→Service（双跳 + SNAT）

外部 LB 指向集群所有节点，流量到达节点后识别为某 Service 并路由到后端 Pod，存在**双跳**（external LB→节点→后端）。NodePort/LoadBalancer 场景下客户端源 IP 会被改写（SNAT 到节点 IP），与 ClusterIP 场景不重写源 IP 的行为不同。

> 要点：源 IP 保留策略——ClusterIP 直连后端不重写源 IP；经 NodePort/LB 进入时默认 SNAT；`externalTrafficPolicy: Local` 可在节点本地转发并保留源 IP。

![[assets/network/diagram-packet-paths.svg]]

*图：三条数据包路径对比——Pod→Pod 全程无 NAT；Pod→Service 经 DNAT + conntrack 逆向转换；外部→Service 双跳且默认 SNAT（Local 策略可保留源 IP）。*

### 6. conntrack 与 TCP 连接跟踪

kube-proxy 的 DNAT/SNAT 全部建立在 Linux conntrack 之上：conntrack 记录连接的五元组与转换状态，回程包据此还原。Linux 内核 6.1 之前存在 conntrack bug，可能导致访问 Service IP 的长连接被以「Connection reset by peer」关闭。

> 要点：iptables 模式默认安装该 bug 的 workaround（但会带来其他问题）；nftables 模式默认不装，可用 `--conntrack-tcp-be-liberal` 选项规避，用 `iptables_ct_state_invalid_dropped_packets_total` 指标判断集群是否依赖 workaround。

### 7. 流量策略：internalTrafficPolicy / externalTrafficPolicy

两个字段控制流量路由范围：`Cluster` 表示路由到所有 ready 后端，`Local` 表示只路由到节点本地 ready 端点。Local 且无本地端点时，kube-proxy 直接丢弃/不转发该 Service 流量。

> 要点：Local 模式的代价是流量分布不均与丢包风险，收益是保留客户端源 IP 与低延迟；Cluster 模式所有节点都是候选负载均衡目标（前提：节点未被删除且 kube-proxy 健康）。

### 8. overlay 封装与 MTU 约束

Flannel/OVS 等 overlay 网络用 VXLAN（UDP 4789）或 GRE 封装内层数据包，在物理网络上传输 Pod 流量；封装头（VXLAN 约 50 字节）占用 MTU 预算，若不调小 Pod 网络 MTU 会出现大包分片、TCP 性能下降甚至黑洞。

> 要点：TCP 三次握手与数据传输必须适配 MTU——overlay 场景需将 Pod 接口 MTU 设为物理 MTU 减去封装头长度（如物理 1500 → VXLAN 1450），这是 K8s 网络性能排障的经典检查项。

![[assets/network/diagram-overlay-mtu.svg]]

*图：VXLAN 封装与 MTU 预算——正确配置下内层包 1450 + 封装头 50 = 外层 1500 无分片；错误配置 1500 + 50 = 1550 超限导致分片/黑洞。*

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方出处 |
|------|------|----------|----------|
| Service 连接失败 / TCP 三次握手无法完成 | 后端 Pod 未就绪（readiness 未通过，endpoint 未进入 ready 状态）、kube-proxy 规则尚未同步（minSyncPeriod 聚合窗口内）、或 iptables 规则被外部组件干扰后 kube-proxy 未及时察觉 | 检查 EndpointSlice/Endpoints 是否列出 ready 后端（`kubectl get endpointslices`）；看 kube-proxy 指标 `sync_proxy_rules_duration_seconds` 是否远超 1s 判断同步瓶颈；minSyncPeriod 默认 1s 在多数集群够用，规则被干扰的问题由 syncPeriod 周期清理兜底 | virtual-ips/ 的 iptables proxy mode 与 Optimizing iptables mode performance 章节 |
| 访问 Service 的长连接被「Connection reset by peer」关闭 | Linux 内核 6.1 之前的 conntrack bug：长时间存活的 TCP 连接到 Service IP 时可能被内核错误关闭；iptables 模式默认安装 workaround，但该 workaround 在某些集群引发其他问题 | nftables 模式下检查 `iptables_ct_state_invalid_dropped_packets_total` 指标确认是否依赖 workaround，若是则以 `--conntrack-tcp-be-liberal` 选项启动 kube-proxy 规避；升级内核到 6.1+ 根治 | virtual-ips/ 的 Migrating from iptables mode to nftables 章节（Conntrack bug workarounds） |
| externalTrafficPolicy: Local 时外部流量被丢弃 / 服务间歇不可达 | Local 策略只路由到节点本地 ready 端点；当节点上没有本地端点（如 Deployment 缩容到 0、滚动更新中 Pod 迁移）时，kube-proxy 不转发该 Service 的任何流量，外部 LB 在健康检查探针间隙仍可能把流量打到该节点 | 启用 `ProxyTerminatingEndpoints` 特性（v1.28+ stable）让 Local 模式下流量可转发给 terminating 端点实现优雅排空；或确保每个节点保留本地副本；或将策略改为 Cluster 接受源 IP 被改写 | virtual-ips/ 的 Traffic policies 与 Traffic to terminating endpoints 章节 |
| 节点删除/滚动更新期间外部流量中断，或 kube-proxy 反复重启 | 把 kube-proxy 的 readiness 探针（/healthz，节点删除时返回 503）误配成 liveness 探针：节点删除时探针持续失败导致 kube-proxy 无限重启直至节点彻底删除；健康检查失败也使 LB 立即断开存量连接 | livenessProbe 使用 `/livez`（只反映网络编程进度、不受节点删除影响），readinessProbe 使用 `/healthz`（节点删除时返回 503 触发 LB connection draining）；用 `proxy_livez_total` / `proxy_healthz_total` 指标观察两类探针状态 | virtual-ips/ 的 External traffic policy 章节（healthz/livez 与节点删除语义） |
| NodePort Service 在 nftables 模式不可达，或与防火墙冲突 | nftables 模式默认 `--nodeport-addresses primary`，NodePort 只监听节点主 IP（iptables 模式默认监听所有本地 IP）；且 iptables 模式会为 NodePort 自动添加放行入站流量的防火墙兼容规则，nftables 模式不做任何事 | 显式指定 `--nodeport-addresses 0.0.0.0/0` 恢复全 IP 监听（或 `primary,localhost` 启用 127.0.0.1 NodePort，需 KubeProxyNFTablesLocalhostNodePorts 特性门控）；自行配置本地防火墙放行整个 NodePort 端口范围 | virtual-ips/ 的 Migrating from iptables mode to nftables 章节 |
| 大集群 Service/Endpoint 变更后 iptables 规则更新慢、连接偶发失败 | iptables 模式为每个 Service 和每个 endpoint IP 各建数条规则，万级 Pod/Service 产生数万条规则；minSyncPeriod: 0s 时每次变更立即全量同步，短时间内大量变更（如删除 100 Pod 的 Deployment）产生上百次更新 | 将 minSyncPeriod 调大（默认 1s 起步）聚合短时间内的变更批量同步；K8s v1.28 起 iptables 模式已改为最小化更新（只更新变化的 Service/EndpointSlice），旧的自定义大 minSyncPeriod 覆盖应移除；用 `sync_proxy_rules_duration_seconds` 指标验证 | virtual-ips/ 的 Optimizing iptables mode performance 章节 |
| Pod 访问集群外部（互联网）时源 IP 被改写，对端无法识别 Pod | Pod 网段在物理网络/云项目外不可路由，Pod 出集群流量必须 SNAT（masquerade）到节点 VM IP 才能被外部网络识别并允许（官方 GCE 实现：pod 流量出项目必须 masquerade） | 属设计行为而非故障：区分 Pod 间流量（NAT-less 直连）与出集群流量（强制 SNAT）；需要外部识别真实来源时改用 egress 网关/代理方案或外部化服务 | design-proposals-archive/network/networking.md 的 Implementation 章节（SNAT'ed to the VM's IP） |

## 最佳实践

1. **按四类网络问题分层理解与排障**：容器间（localhost）、Pod 间、Pod-Service、外部-Service 四类问题由不同组件解决（Pod/locahost、CNI 插件、kube-proxy、LB/Ingress），排障时先定位流量处于哪一段路径，避免在错误层次浪费时间。
2. **三段 IP 规划保持非重叠，dual-stack 时主 IP 族全局一致**：Pod、Service、Node 的 IP 范围由网络插件、kube-apiserver、kubelet/CCM 分别配置且必须互不重叠；IPv4/IPv6 dual-stack 下所有组件必须协商一致的主 IP 族。
3. **网络插件必须符合 CNI v0.4.0+，推荐 v1.0.0+**：K8s 要求 CNI 插件兼容 v0.4.0 或更高版本规范，官方推荐兼容 v1.0.0 的插件（插件可同时兼容多个规范版本）；K8s 1.24 起 kubelet 不再管理 CNI（cni-bin-dir/network-plugin 参数已移除），由容器运行时负责加载插件。
4. **iptables 模式性能参数按指标调优，v1.28+ 不要覆盖 minSyncPeriod**：minSyncPeriod 默认 1s 适合绝大多数集群；`sync_proxy_rules_duration_seconds` 均值远大于 1s 时才考虑调大；v1.28 起 iptables 模式采用最小化更新，旧的大 minSyncPeriod 覆盖应移除，syncPeriod 不要设 1h 这类极端值。
5. **kube-proxy 探针分离：liveness 用 /livez，readiness 用 /healthz**：/healthz 在节点删除时返回 503（支持 LB connection draining），/livez 只反映网络编程进度；把 healthz 配成 liveness 会导致节点删除期间 kube-proxy 重启循环，云厂商/自实现 VIP 应提供类似健康检查端口。
6. **流量策略选择：需要保留源 IP 用 Local，需要均匀负载用 Cluster**：externalTrafficPolicy: Local 保留客户端源 IP 且路径最短，但无本地端点时丢包且分布不均；Cluster 全节点可负载但源 IP 被改写。滚动更新场景配合 `ProxyTerminatingEndpoints`（v1.28+ stable）实现优雅排空。
7. **拓扑感知路由用 trafficDistribution 而非手工干预**：`service.spec.trafficDistribution` 支持 PreferSameZone / PreferSameNode（PreferClose 已废弃），表达路由到拓扑更近端点的偏好（性能/成本/可靠性优化），比流量策略的严格语义更灵活。
8. **hostPort 与带宽整形使用官方 meta 插件**：hostPort 能力用官方 portmap 插件并在 CNI 配置声明 `portMappings` capability（配合 `externalSetMarkChain: KUBE-MARK-MASQ`）；Pod 出入带宽限制用 bandwidth 插件 + `kubernetes.io/ingress-bandwidth` / `egress-bandwidth` 注解（experimental 特性）。

## 排查命令

```bash
# Service 端点与 VIP 状态
kubectl get endpointslices -l kubernetes.io/service-name=<svc>
kubectl get endpointslices -o jsonpath='{.items[*].endpoints[*].conditions.ready}'

# kube-proxy 规则同步性能（>1s 即瓶颈）
kubectl -n kube-system get pod -l k8s-app=kube-proxy -o wide
curl -s localhost:10249/metrics | grep sync_proxy_rules_duration_seconds

# conntrack workaround 依赖判断（nftables 模式）
curl -s localhost:10249/metrics | grep iptables_ct_state_invalid_dropped_packets_total

# kube-proxy 探针状态（区分 livez / healthz 语义）
curl -s localhost:10249/metrics | grep -E "proxy_livez_total|proxy_healthz_total"

# iptables 模式：查看 Service 规则链
iptables-save | grep -E "KUBE-SVC|KUBE-SEP" | head -20
# nftables 模式：查看规则集
nft list ruleset | grep -A 5 "kube-proxy"

# conntrack 连接跟踪表（看 DNAT 转换是否生效）
conntrack -L | grep <service-vip>

# MTU 检查（overlay 场景：Pod 接口 MTU = 物理 MTU - 封装头）
ip link show <eth>            # 物理网卡 MTU（节点上）
kubectl exec <pod> -- ip link show eth0   # Pod 接口 MTU
# 期望：物理 1500 → VXLAN 场景 Pod 1450；不一致则查网络插件 MTU 配置
```

## 相关笔记

- [[k8s-cluster-networking]] — 集群网络全景（CNI/Service/DNS/NetworkPolicy），网络入门的整体地图
- [[k8s-network-dns-lb-cni]] — DNS 解析 / 负载均衡 / CNI 模型深潜（控制面与规则视角）
- 本篇为数据面视角：数据包流转路径、conntrack、MTU、源 IP 保留策略，与上述两篇互补

## 官方参考

- [Cluster Networking（集群网络模型与 IP 规划）](https://kubernetes.io/docs/concepts/cluster-administration/networking/)
- [Virtual IPs and Service Proxies（kube-proxy 模式/数据包处理/流量策略/conntrack）](https://kubernetes.io/docs/reference/networking/virtual-ips/)
- [Service（类型/VIP/流量分发/会话粘性）](https://kubernetes.io/docs/concepts/services-networking/service/)
- [Network Plugins（CNI 要求/hostPort/带宽整形）](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/)
- [Kubernetes 网络设计文档（官方早期设计：NAT-less 扁平空间/数据包流转/SNAT）](https://git.k8s.io/design-proposals-archive/network/networking.md)
- [CNI Specification 1.1.0](https://github.com/containernetworking/cni/blob/main/SPEC.md)
