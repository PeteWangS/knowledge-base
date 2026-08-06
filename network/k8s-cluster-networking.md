---
created: 2026-08-06
tags: [network, kubernetes, cni, service, dns]
topic: 网络
---

# Kubernetes 集群网络全景：CNI / Service / DNS / NetworkPolicy

> 本轮研究主题：TCP/IP 基础、DNS 解析原理、负载均衡算法、容器网络 CNI 模型 —— 聚焦 **Kubernetes 集群网络四层模型**（官方文档视角）。

## 概述

Kubernetes 集群网络解决四类问题：

| # | 问题 | 解决方案 |
|---|------|---------|
| ① | 容器间通信（同一 Pod 内） | localhost，由容器运行时 + 共享网络命名空间解决 |
| ② | Pod 间通信 | 集群扁平网络，由 CNI 插件实现（任意 Pod IP 全网可路由，**无 NAT**） |
| ③ | Pod 到 Service | kube-proxy 虚拟 IP + DNAT 转发 |
| ④ | 外部到 Service | NodePort / LoadBalancer / Ingress 入口 |

Kubernetes 采用**三段独立、互不重叠**的 IP 地址空间：Pod IP（网络插件分配）、Service IP（kube-apiserver 分配）、Node IP（kubelet / cloud-controller-manager 分配）。集群按 IP 族分为 IPv4-only、IPv6-only、dual-stack 三类，所有组件必须对主 IP 族达成一致。网络模型由各节点上的容器运行时实现，主流运行时通过 **CNI（Container Network Interface）** 插件管理网络，当前规范版本 1.1.0。

## 架构图

![[assets/network/diagram-k8s-network-arch.svg]]

**四层叠加结构**：Pod 内 localhost 互访 → CNI 扁平 Pod 网络（veth + 网桥/Overlay）→ Service VIP 层（kube-proxy DNAT + CoreDNS 记录）→ 外部入口层（NodePort / LB / Ingress）。

![[assets/network/diagram-cni-service-flow.svg]]

**CNI 协议与 Service 流量路径**：CNI 定义配置 JSON 格式、六种执行操作（ADD/DEL/CHECK/STATUS/VERSION/GC）与插件链（main 插件 + meta 插件 + IPAM 插件）；Service 由 apiserver 分配 VIP，kube-proxy 安装转发规则做 DNAT。

## 核心概念

### 1. Kubernetes 网络模型（四类网络问题）
集群网络需要解决的 4 个独立问题：容器间通信（Pod 内 localhost）、Pod 间通信（扁平网络直连）、Pod 到 Service（VIP 代理）、外部到 Service（入口暴露）。共享机器时不再靠人工协调端口，而是用网络抽象隔离。

> **关键点**：Pod 间通信不经过 NAT，任意 Pod IP 全网可路由；端口协调问题由 Service 抽象解决。

### 2. CNI（Container Network Interface）
CNI 是容器运行时与网络插件之间的接口规范，定义网络配置 JSON 格式、执行协议（ADD 添加容器入网、DEL 移除、CHECK 校验、STATUS 状态探测、VERSION 版本协商、GC 垃圾回收）与插件委托机制（主插件可委托 IPAM 插件分配地址）。

> **关键点**：Kubernetes 要求 CNI 插件兼容 v0.4.0 以上规范，官方推荐 v1.0.0 及以上；kubelet 自 1.24 起不再管理 CNI，改由容器运行时加载。

### 3. Service 与虚拟 IP（VIP）
Service 由 kube-apiserver 从 `service-cluster-ip-range` 分配虚拟 IP，该 IP 不实际由单一主机应答；kube-proxy 用 iptables/nftables 等内核包处理逻辑定义 VIP，将流量 DNAT 到后端端点。环境变量与 DNS 均以 VIP 形式发布。

> **关键点**：VIP 分配由 etcd 中的全局分配表原子更新保证唯一，避免用户自行选 IP 导致冲突。

### 4. Service 四种类型
- **ClusterIP**（默认，仅集群内可达）
- **NodePort**（每节点固定端口 30000-32767 转发；静态带 30000-30085 + 动态带 30086-32767）
- **LoadBalancer**（对接外部负载均衡器，基于 ClusterIP/NodePort 嵌套）
- **ExternalName**（DNS CNAME 映射外部主机名，无代理）

> **关键点**：类型设计为嵌套结构，每层在前一层基础上叠加；LoadBalancer 可禁用 NodePort 分配作为例外。

### 5. kube-proxy 代理模式
| 模式 | 说明 | 备注 |
|------|------|------|
| iptables | 默认，每 Service 若干规则 + 每端点若干规则 | 随机选后端；大集群规则数达数万条时同步变慢 |
| ipvs | 哈希表数据结构，支持 11 种调度算法 | **v1.35 起弃用**（IPVS API 与 Service API 边角语义不匹配） |
| nftables | iptables API 的继任者，性能更好 | 要求内核 5.13+，是 ipvs 的推荐替代 |

Windows 仅有 kernelspace 模式。

### 6. 负载均衡调度算法（IPVS）
IPVS 模式支持 rr（轮询）、wrr（加权轮询）、lc（最少连接）、wlc（加权最少连接）、lblc/lblcr（基于局部性的最少连接）、sh（源地址哈希）、dh（目的地址哈希）、sed（最短期望延迟）、nq（永不排队）、mh（Maglev 一致性哈希）等算法，通过 `ipvs.scheduler` 字段指定。

> **关键点**：iptables/nftables 模式默认随机选后端；IPVS 的 sh/mh 算法天然支持会话保持场景。

### 7. 集群 DNS 与 CoreDNS
CoreDNS 监听 API 为 Service 与 Pod 生成 DNS 记录：普通 Service 的 A/AAAA 记录解析到 ClusterIP，headless Service 解析到全部 Pod IP 集合，命名端口生成 SRV 记录。Pod 的 resolv.conf 含 nameserver、search 域（`namespace.svc.cluster.local svc.cluster.local cluster.local`）与 `options ndots:5`。

> **关键点**：DNS 查询按 Pod 所在命名空间限定；跨命名空间须用 FQDN（如 `data.prod.svc.cluster.local`）；dnsPolicy 支持 Default / ClusterFirst / ClusterFirstWithHostNet / None 四种。

### 8. NetworkPolicy 网络隔离
NetworkPolicy 通过 podSelector 选择目标 Pod 组，policyTypes 声明 Ingress/Egress，规则用 from/to 选择器（podSelector、namespaceSelector、ipBlock）限定流量来源/去向与端口。无策略时命名空间内默认全部放行，创建策略即触发隔离。

> **关键点**：网络插件必须支持 NetworkPolicy 才生效；hostNetwork Pod 的 NetworkPolicy 行为未定义（多数实现按节点流量处理）。

### 9. 会话亲和与流量策略
Service 支持 `sessionAffinity: ClientIP` 将同一客户端固定到同一 Pod（默认超时 10800 秒）；internalTrafficPolicy / externalTrafficPolicy 可取 Cluster（路由到全部就绪端点）或 Local（仅节点本地端点），Local 保留客户端源 IP 但无本地端点时丢流量。

> **关键点**：`trafficDistribution: PreferSameZone` 表达路由偏好（如优先同可用区端点）；Local 模式配合 kube-proxy 10256/healthz 健康检查实现 LB 摘流。

## 常见问题表

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| 客户端 Pod 通过环境变量找不到 Service（SERVICE_HOST 为空） | kubelet 只在 Pod 创建时注入当时已存在的 Service 变量；Service 后创建不补齐 | 改用 DNS 服务发现；若必须用环境变量，先建 Service 再建 Pod |
| iptables 模式大集群规则同步慢 | 每个 Service/端点都生成规则，总量数万条；变更全量同步 CPU 开销大 | 调整 `iptables.minSyncPeriod`；v1.28 起仅更新变更规则；大集群迁 nftables（内核 5.13+） |
| ipvs 模式启动失败（IPVS 内核模块缺失） | 依赖内核 IPVS 模块；检测不到即报错退出 | 启动前 `modprobe ip_vs` 系列模块；新集群优先 nftables 或 iptables |
| externalTrafficPolicy: Local 时部分节点访问失败 | Local 只路由到节点本地端点；无本地端点时 kube-proxy 直接丢弃 | 每节点有副本（DaemonSet 式）；或 Cluster 策略；启用 ProxyTerminatingEndpoints 优雅排空 |
| NetworkPolicy 对 hostNetwork Pod 不生效 | 规范未定义：多数插件将其当节点流量处理，podSelector 匹配不到 | 用 ipBlock 按节点 IP 放行；配 `dnsPolicy: ClusterFirstWithHostNet` |
| setHostnameAsFQDN: true 后 Pod 一直 Pending | Linux 内核 hostname 限长 64 字符；FQDN 超长无法构造 | 控制 FQDN ≤ 64 字符；或准入 webhook 校验 |
| Pod 大量 DNS search 域卡 Pending | search 域 >32 个或总长 >2048 被拒；旧运行时（containerd ≤1.5.5、CRI-O ≤1.21）有自身限制 | 升级运行时；控制 dnsConfig.searches 数量与长度 |
| Pod 解析跨命名空间 Service 名失败 | DNS 查询不指定命名空间则限定在自身命名空间 | 用 FQDN：`data.prod` 或 `data.prod.svc.cluster.local` |
| 修改 NetworkPolicy 后已建连接未断 | 对现有连接的影响是插件实现定义的：有的立即切断，有的仅影响新连接 | 不在活跃连接期间修改策略/标签；配合就绪探针与滚动更新平滑切换 |

## 最佳实践

1. **优先使用 DNS 而非环境变量做服务发现** —— 环境变量依赖创建顺序且不支持存量 Pod 更新；集群内几乎总应部署 CoreDNS。
2. **大规模集群评估 nftables 代理模式** —— iptables 在数万 Service/端点规模下同步开销大；nftables 是官方推荐替代（内核 5.13+，注意与网络插件兼容性）。
3. **需要保留客户端源 IP 时用 externalTrafficPolicy: Local** —— Cluster 会 SNAT 丢失源 IP；Local 要求节点上有本地端点，LB 健康检查对准 `:10256/healthz`。
4. **kube-proxy 存活探针用 /livez 而非 /healthz** —— /healthz 在节点删除时返回 503（支持连接排空），作 livenessProbe 会导致无限重启。
5. **默认拒绝所有入站流量 + 显式放行（default deny）** —— 创建「选择全部 Pod 且不允许任何入站」的策略作基线；ipBlock 只用于集群外部 IP（Pod IP 是临时的）。
6. **选择兼容 CNI v1.0.0+ 规范的网络插件** —— hostPort 需 portmap 插件（portMappings capability），带宽整形需 bandwidth 插件。

## 排查命令

```bash
# Service 与端点
kubectl get svc -A
kubectl get endpointslices -A
kubectl describe svc <service>

# DNS 排查
kubectl -n kube-system get pods -l k8s-app=kube-dns
kubectl run -it --rm debug --image=busybox:1.28 -- nslookup <service>.<namespace>
kubectl exec -it <pod> -- cat /etc/resolv.conf

# NetworkPolicy
kubectl get networkpolicy -A
kubectl describe networkpolicy <name>

# kube-proxy 健康与规则
curl http://<node-ip>:10256/healthz
iptables -t nat -L KUBE-SERVICES -n

# 节点网络
ip addr show cni0
ip route
```

## 相关笔记

- 待补：`[[k8s-cluster-ops-combat]]`（K8s 实战：集群排错方法论）
- 待补：`[[k8s-pod-basics]]`（K8s 基础：Pod 生命周期与调度）

## 官方参考

- [Cluster Networking](https://kubernetes.io/docs/concepts/cluster-administration/networking/)
- [Services](https://kubernetes.io/docs/concepts/services-networking/service/)
- [DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/)
- [Network Policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- [Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/)
- [Network Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/)
- [CNI Specification 1.1.0](https://github.com/containernetworking/cni/blob/main/SPEC.md)
- [Ingress](https://kubernetes.io/docs/concepts/services-networking/ingress/)
