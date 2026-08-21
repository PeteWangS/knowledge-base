---
created: 2026-08-21
tags: [network, kubernetes, dns, cni, kube-proxy, service]
topic: 网络
---

# Kubernetes 网络深潜：DNS 解析 / 负载均衡 / CNI 模型

> 本轮研究主题：TCP/IP 基础、DNS 解析原理、负载均衡算法、容器网络 CNI 模型 —— 聚焦 **DNS 服务发现、kube-proxy 代理模式与 CNI 插件规范**（官方文档细节视角）。全景四层模型见 `[[k8s-cluster-networking]]`。

## 概述

Kubernetes 网络建立在 TCP/IP 协议栈之上，其核心是一套自洽的虚拟网络模型：Pod 是网络最小单元（同一 Pod 内容器共享网络命名空间，通过 localhost 通信），Pod 之间可直接互通（不依赖 NAT），Service 提供 VIP 负载均衡抽象，外部流量经 NodePort/LoadBalancer/Ingress 进入集群。官方文档将集群网络归纳为 **4 类问题**：容器间通信（Pod + localhost 解决）、Pod 间通信（CNI 插件解决）、Pod 到 Service（kube-proxy 规则解决）、外部到 Service（Service 类型解决）。集群需为 Pod、Service、Node 分配三段互不重叠的 IP 网段。DNS 服务发现、负载均衡算法（iptables/nftables 随机选后端、IPVS 11 种调度器）与 CNI 插件链（ADD/DEL/CHECK/STATUS/VERSION/GC 六操作）共同构成完整的解析链路。

## 架构图

![[assets/network/diagram-network-4layer-arch.svg]]

*图：Kubernetes 四层网络架构 —— 容器间 localhost → CNI 扁平 Pod 网络 → kube-proxy VIP 层 → 外部入口层（NodePort/LoadBalancer/Ingress）。*

**四层职责边界**：① 容器到容器：同一 Pod 内容器共享网络命名空间与 IP，通过 localhost 通信，无需路由；② Pod 到 Pod：容器运行时调用 CNI 插件（如 bridge 创建 veth + 网桥、calico 叠加网络）为每个沙箱创建接口并分配 IP，插件链可串联（如 bridge + tuning + portmap 组合），IPAM 由委托的 IPAM 插件（host-local/dhcp）负责；③ Pod 到 Service：kube-apiserver 为 Service 分配 VIP，集群内每个节点的 kube-proxy 依据代理模式（iptables/ipvs/nftables）安装转发规则，将访问 VIP:Port 的报文 DNAT 到后端 Pod 端点，默认随机选后端，可配会话亲和；④ 外部到 Service：NodePort 在每个节点监听静态端口转发，LoadBalancer 由云厂商控制器在 NodePort 之上挂外部 LB 并做健康检查，Ingress 提供 HTTP/HTTPS 七层路由（主机名/路径转发、TLS 终止）。

## 核心概念

### 1. Kubernetes 网络模型与四类网络问题

官方将集群网络划分为 4 个独立问题：容器间通信（Pod 内 localhost）、Pod 间通信、Pod 到 Service、外部到 Service。模型要求 Pod 间可直接通信、无需端口协调，避免跨开发者手工分配端口的规模瓶颈。**网络模型由每节点上的容器运行时实现**，最常见方式即 CNI 插件；K8s 只定义模型与接口，不内置实现。

### 2. 三段 IP 地址规划（Pod/Service/Node）

集群需为三类对象分配互不重叠的地址：网络插件分配 Pod IP、kube-apiserver 分配 Service IP、kubelet/cloud-controller-manager 分配 Node IP。仅考虑对象 status 中登记的 IP（pod.status.ips / node.status.addresses），与网卡上实际存在的多 IP 无关。IPv4/IPv6 双栈时所有组件必须就主 IP 族达成一致。**三段 CIDR 必须不重叠，否则路由冲突**；双栈集群主 IP 族不一致会导致网络模型失效。

### 3. Service 四种类型

- **ClusterIP**（默认）：集群内 VIP，可用 Ingress/Gateway 暴露公网
- **NodePort**：每节点静态端口 30000-32767，转发到 ready 端点
- **LoadBalancer**：云厂商外部 LB，异步创建，状态写 .status.loadBalancer，典型实现 = NodePort + 云控制器
- **ExternalName**：DNS CNAME 映射外部主机名，无任何代理

类型设计为嵌套：NodePort 建立在 ClusterIP 之上，LoadBalancer 建立在 NodePort 之上。⚠️ LoadBalancer 默认 `allocateLoadBalancerNodePorts=true`；设 false 后已有 NodePort 不会自动回收，需显式删除端口条目。

### 4. DNS 记录与服务发现

![[assets/network/diagram-dns-resolution-chain.svg]]

*图：DNS 服务发现解析链路 —— CoreDNS 依据 API 生成记录，resolv.conf search 域链 + ndots 实现简名解析，跨命名空间必须用 FQDN。*

Service 获得 A/AAAA 记录（my-svc.my-ns.svc.cluster.local → ClusterIP），Headless Service 同域名解析为全部后端 Pod IP 集合（客户端自选或轮询）；命名端口生成 SRV 记录（_port-name._port-protocol.my-svc...）。Pod 有 hostname/subdomain 字段可自定义 FQDN；setHostnameAsFQDN=true 时 hostname 命令直接返回 FQDN。发现机制两种：环境变量（{SVCNAME}_SERVICE_HOST/PORT，需 Service 先于 Pod 创建）与 DNS（推荐，无顺序问题）。**跨命名空间访问必须用 FQDN**（my-svc.my-ns.svc.cluster.local）；同命名空间才可用简名 my-svc。

### 5. dnsPolicy 四态与 dnsConfig

| 策略 | 行为 |
|------|------|
| Default | 继承节点 resolv.conf |
| ClusterFirst | 默认值，非匹配 cluster 域后缀的查询转发上游（不写 dnsPolicy 时默认即 ClusterFirst 而非 Default） |
| ClusterFirstWithHostNet | hostNetwork Pod 必须显式设置，否则回退 Default 行为 |
| None | 完全由 dnsConfig 提供，此时 dnsConfig 必填 |

dnsConfig 可追加 nameservers（≤3 个）、searches、options，与策略生成的基础配置去重合并。⚠️ **search 域上限 32 个、总长上限 2048 字符**；超过后节点解析配置/合并配置均受限。

### 6. kube-proxy 代理模式与负载均衡算法

![[assets/network/diagram-proxy-modes.svg]]

*图：kube-proxy 三种代理模式 —— iptables 默认 / ipvs 已弃用 / nftables 推荐，共同转发到后端 Pod 端点。*

Linux 三模式：

- **iptables**（默认）：per-endpoint 规则，默认随机选后端，v1.28 起增量同步
- **ipvs**：哈希表内核态，支持 11 种调度器（rr/wrr/lc/wlc/lblc/lblcr/sh/dh/sed/nq/mh），**v1.35 起 deprecated**
- **nftables**：iptables 后继，性能与扩展性更优，需内核 5.13+，官方推荐替代 ipvs

每个 Service 由控制面分配 VIP；externalTrafficPolicy/internalTrafficPolicy 控制 Cluster（全部 ready 端点）或 Local（仅节点本地端点）路由。⚠️ **ipvs 模式从未完整实现 Service 语义**（官方定性 kernel IPVS API 与 K8s Services API 不匹配），新集群应选 nftables 或 iptables。

### 7. CNI 规范与插件链

CNI 定义：管理员网络配置 JSON 格式（cniVersion/name/plugins/capabilities/ipam/dns）、运行时与插件间的执行协议、插件执行流程、插件委托（IPAM 等）与结果类型。**六大操作**经 CNI_COMMAND 环境变量传入：

| 操作 | 职责 |
|------|------|
| ADD | 创建/调整接口 |
| DEL | 删除/撤销，best-effort 且必须幂等接受重复调用 |
| CHECK | 校验容器网络与 prevResult 一致 |
| STATUS | 就绪探活，错误码 50/51 |
| VERSION | 版本协商 |
| GC | 按 cni.dev/valid-attachments 清理陈旧资源，不得替代 DEL |

运行时选择 cniVersion/cniVersions 中最高支持版本。⚠️ K8s 要求 CNI 插件兼容 spec v0.4.0+（推荐 v1.0.0+）；**kubelet 自 1.24 起不再管理 CNI**（cni-bin-dir/network-plugin 参数移除），移交容器运行时。

### 8. NetworkPolicy 隔离模型

NetworkPolicy 是命名空间级资源，双向隔离：ingress 规则匹配来源（podSelector/namespaceSelector/ipBlock）、egress 规则匹配去向，可限定端口与协议。默认（无任何策略）命名空间内所有入站出站放行；策略语义为**默认拒绝 + 显式 allow**（无 deny 动作）。hostNetwork Pod 的行为未定义，多数实现按节点流量处理。⚠️ 默认 deny 全部 egress 会连带阻断 DNS，必须额外放行 kube-dns；NetworkPolicy API 不支持按 Service 名或节点身份定位。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|----------|----------|
| Pod 内无法解析其他命名空间的 Service 简名（nslookup 失败） | DNS search 域链只含本命名空间，简名 my-svc 只在本命名空间内有效 | 跨命名空间一律用完整 FQDN my-svc.my-ns.svc.cluster.local；或配置 dnsConfig.searches 追加目标命名空间 search 域 | dns-pod-service/（Pods DNS Policy 与 search 域） |
| Pod 卡在 Pending（ContainerCreating）无法启动 | search 域数量超过 32 个或总长度超过 2048 字符；containerd v1.5.5 及更早、CRI-O v1.21 及更早限制更严 | 控制 dnsConfig.searches 在 32 个/2048 字符内；升级 containerd/CRI-O；限制超长域名层级 | dns-pod-service/（DNS search domain list limits） |
| 启用 setHostnameAsFQDN 后 Pod 启动失败（Pending + Failed to construct FQDN 事件） | Linux 内核 hostname 限制 64 字符；FQDN 超长时 kubelet 无法写入 hostname | 缩短域名层级或命名；或用准入 webhook 校验 FQDN 长度 | dns-pod-service/（setHostnameAsFQDN 字段说明） |
| hostNetwork Pod 的 DNS 解析行为异常（走了节点配置而非集群 DNS） | hostNetwork Pod 用默认 ClusterFirst 会回退到 Default 策略行为（继承节点 resolv.conf） | hostNetwork Pod 显式设置 dnsPolicy: ClusterFirstWithHostNet（Windows 节点不支持） | dns-pod-service/（Pod's DNS Policy） |
| externalTrafficPolicy: Local 的 Service 流量黑洞（部分节点访问失败） | Local 策略只路由到节点本地 ready 端点；请求落在无本地端点的节点时 kube-proxy 直接丢弃流量 | 确保每个可能收到流量的节点都有本地端点（DaemonSet 式部署）；或改回 Cluster 策略；滚动更新可启用 ProxyTerminatingEndpoints 优雅排空 | virtual-ips/（Internal/External traffic policy） |
| 删除节点时 LoadBalancer 流量瞬间中断/连接被掐断 | 云控制器在节点删除完成时立即移出 LB 后端并终止全部连接；未配就绪探针则不会提前排空 | LB 健康检查指向 kube-proxy 就绪端口 ${NODE_IP}:10256/healthz（删除中返回 503 触发排空）；livenessProbe 应用 /livez 而非 /healthz | virtual-ips/（Traffic policies 节点删除） |
| kube-proxy ipvs 模式启动即退出或 Service 行为异常 | 节点未预装 IPVS 内核模块则退出；且该模式 v1.35 起官方弃用——kernel IPVS API 无法完整实现 Service 边界语义 | 新集群改用 nftables（内核 5.13+）或 iptables；已用 ipvs 的规划迁移 | virtual-ips/（IPVS proxy mode） |
| 应用默认 deny 全部 egress 的 NetworkPolicy 后 Pod 无法解析域名 | 默认拒绝 egress 同时阻断了对 kube-dns/CoreDNS 的 UDP/TCP 53 流量 | egress 规则显式放行 DNS：allow to kube-system + 53 端口；或只对需要隔离的工作负载应用策略 | network-policies/（Default deny all egress caution） |
| Service 环境变量方式发现失败（客户端连不上服务） | kubelet 只在 Pod 创建时注入当时已存在的 Service 环境变量，晚创建则缺失且不补发 | 优先用 DNS 服务发现；必须用环境变量时先创建 Service 再创建客户端 Pod | service/（Discovering services note） |
| LoadBalancer Service 多端口协议不同被拒绝/行为异常 | 默认要求多端口协议一致；混合协议需 MixedProtocolLBService gate（v1.24 起默认开启），云厂商可能另有限制 | 升级到 v1.24+ 或拆分为多个 Service；核对云厂商 LB 协议支持矩阵 | service/（Load balancers with mixed protocol types） |
| 大规模集群 iptables 模式规则同步慢、流量更新延迟 | iptables 模式为每个 Service/端点安装规则，万级 Pod 集群规则数万条 | kube-proxy 配置 iptables.minSyncPeriod（默认 1s）与 syncPeriod；v1.28 起默认增量更新；或用 nftables 模式 | virtual-ips/（Optimizing iptables mode performance） |
| 升级到 Kubernetes 1.24+ 后 CNI 网络不工作（dockershim 移除） | kubelet 的 cni-bin-dir/network-plugin 参数在 1.24 移除，CNI 管理不再属于 kubelet 职责 | 由容器运行时（containerd/CRI-O）负责加载 CNI 插件；插件需兼容 CNI spec v0.4.0+ | network-plugins/（Installation note） |

## 最佳实践

1. **按官方四类问题模型规划网络，三段 IP 网段互不重叠**：先明确容器间/Pod 间/Pod-Service/外部-Service 四层各自由谁解决；为 Pod（网络插件）、Service（apiserver --service-cluster-ip-range）、Node（kubelet/CCM）规划独立 CIDR；双栈集群所有组件主 IP 族保持一致。官方参考：cluster-administration/networking/
2. **服务发现优先 DNS，跨命名空间用 FQDN**：集群内一律部署 CoreDNS 类 DNS 插件；客户端访问其他命名空间 Service 使用 my-svc.my-ns.svc.cluster.local 全名；避免依赖环境变量发现（有创建顺序约束）。官方参考：service/（Discovering services）
3. **kube-proxy 探针区分就绪与存活：liveness 用 /livez**：外部 LB 健康检查指向 /healthz（10256 端口，节点删除时返回 503 支持连接排空）；kube-proxy 自身 livenessProbe 必须用 /livez（不考虑节点删除状态），否则节点删除期间探针失败导致重启循环。官方参考：virtual-ips/（Traffic policies）
4. **NetworkPolicy 最小权限：默认拒绝 + 显式放行 + 记住放行 DNS**：命名空间入口先建 default-deny（ingress+egress）策略，再按业务显式 allow；任何默认拒绝 egress 的命名空间必须同时放行 kube-dns 53 端口，否则全部域名解析失败。官方参考：network-policies/（Default policies）
5. **CNI 选型与配置规范：版本 ≥v0.4.0、能力用 capabilities 声明**：插件兼容 CNI spec v0.4.0+（推荐 v1.0.0+）；hostPort 用官方 portmap 插件并在 cni-conf-dir 声明 portMappings capability，带宽整形用 bandwidth 插件 + ingress/egress-bandwidth 注解；kubelet 1.24+ 由容器运行时统一管理 CNI。官方参考：network-plugins/ + github.com/containernetworking/cni SPEC.md
6. **利用 CNI GC 与 STATUS 做网络健康管理**：运行时定期 GC 清理崩溃容器遗留的 IPAM 保留与防火墙规则（不得用 GC 替代 DEL）；STATUS 用于探活插件依赖的外部服务；已知插件链组合 CHECK 会误报时用 disableCheck 关闭。官方参考：github.com/containernetworking/cni SPEC.md（Section 2）

## 排查命令

```bash
# DNS 解析排查：确认 search 域链与 nameserver 注入
kubectl exec -it <pod> -- cat /etc/resolv.conf
kubectl exec -it <pod> -- nslookup my-svc.my-ns.svc.cluster.local
kubectl exec -it <pod> -- nslookup my-svc   # 简名仅本命名空间有效

# Service 端点与 VIP 状态
kubectl get svc -A -o wide
kubectl get endpointslices -n <ns>
kubectl describe svc <svc>

# CoreDNS 健康与记录生成
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=50

# kube-proxy 规则检查（按代理模式）
kubectl get cm -n kube-system kube-proxy -o yaml | grep mode
iptables-save | grep <cluster-ip>        # iptables 模式
ipvsadm -Ln                              # ipvs 模式
nft list ruleset | grep <cluster-ip>     # nftables 模式

# 流量策略与端点分布
kubectl get svc <svc> -o yaml | grep -E "externalTrafficPolicy|internalTrafficPolicy"
kubectl get endpointslices -n <ns> <svc> -o yaml

# NetworkPolicy 生效检查
kubectl get netpol -A
kubectl describe netpol -n <ns> <name>
```

## 相关笔记

- `[[k8s-cluster-networking]]` — Kubernetes 集群网络全景（四层模型总览，本笔记为其官方细节深潜）
- `[[k8s-pod-deployment-service]]` — Pod / Deployment / Service 基础
- `[[k8s-core-objects-design]]` — K8s 核心对象设计原理

## 官方参考

- [Cluster Networking（四类网络问题与 IP 规划）](https://kubernetes.io/docs/concepts/cluster-administration/networking/)
- [DNS for Services and Pods（记录类型/dnsPolicy/dnsConfig/search 域限制）](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/)
- [Virtual IPs and Service Proxies（代理模式/IPVS 调度器/流量策略）](https://kubernetes.io/docs/reference/networking/virtual-ips/)
- [Service（四种类型/Headless/服务发现）](https://kubernetes.io/docs/concepts/services-networking/service/)
- [Network Policies（隔离模型/默认策略/hostNetwork）](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- [Network Plugins（CNI 要求/hostPort/带宽整形）](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/)
- [Ingress（七层路由/负载均衡）](https://kubernetes.io/docs/concepts/services-networking/ingress/)
- [CNI 规范 1.1.0（Container Network Interface Specification）](https://github.com/containernetworking/cni/blob/main/SPEC.md)
