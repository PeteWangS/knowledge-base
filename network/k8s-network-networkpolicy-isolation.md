---
created: 2026-09-04
tags: [k8s, network, networkpolicy, calico, cilium, security, isolation]
---

# K8s NetworkPolicy 网络隔离深潜：隔离模型 / 选择器语义 / 默认拒绝策略 / CNI 实现与排错

## 概述

NetworkPolicy 是 Kubernetes 原生的**命名空间级 L3/L4 防火墙对象**（`networking.k8s.io/v1`），只处理 TCP/UDP/SCTP 连接，不感知 L7。它描述「哪些 Pod 在什么条件下可以与谁通信」，但**自身不做任何封包过滤**——真正执行策略的是 CNI 网络插件（Calico / Cilium / kube-router / Antrea 等）：API Server 只负责存储与校验对象，**没有实现 NetworkPolicy 的插件时创建策略毫无效果**（如 Flannel 无实现）。

核心心智模型是「**默认放行 + 隔离后只允许白名单**」：默认情况下 Pod 对入站/出站均非隔离（一切连接放行）；一旦有 NetworkPolicy 选中某 Pod 且在其 `policyTypes` 中声明了某方向，该 Pod 在该方向即被隔离，只允许被各策略 allow 规则显式放行的连接，应答流量隐式放行。

- **策略之间不冲突、纯叠加**：同一方向多个策略的放行集合取并集，求值顺序不影响结果
- **一条连接必须同时通过源 Pod 的 egress 策略与目标 Pod 的 ingress 策略才可达**
- API 只有 allow 规则没有 deny 规则，「默认拒绝」是通过创建一条选中全部 Pod 但不带任何规则的空策略实现的

与 [[k8s-cluster-networking]]（全景视角）、[[k8s-network-dns-lb-cni]]（DNS/LB/CNI 控制面视角）、[[k8s-network-dataplane-tcpip]]（数据面视角）互补：本篇聚焦**隔离语义与策略编写**本身。

## 架构图

![[assets/network/diagram-networkpolicy-arch.svg]]

*图：NetworkPolicy 三层架构——声明层（kubectl/API Server/etcd）只存储校验；控制器层（Calico felix+typh、Cilium cilium-agent）watch 策略与标签变化并编译为节点数据面规则；数据面层在 Pod veth 与节点过滤点交界处逐包判定。标签（Pod 标签、Namespace 标签 `kubernetes.io/metadata.name`）是选择器与策略的关联纽带。*

## 核心概念

### 双向独立隔离模型（isolation）

Pod 的 ingress 与 egress 隔离**相互独立声明**。默认两个方向都非隔离（全部放行）。只要存在某条 NetworkPolicy 同时选中该 Pod 且 `policyTypes` 含 `Egress`，该 Pod 即被 egress 隔离——只允许各适用策略 egress 列表放行的出站连接；ingress 同理。被隔离方向的原先默认放行即失效。

> **关键点**：`policyTypes` 省略时默认 `Ingress` 恒被设置，仅当策略含 egress 规则时才自动加 `Egress`——想单独隔离出站必须显式声明 `policyTypes: [Egress]`。

### 策略叠加语义（additive union）

NetworkPolicy 之间不冲突、纯叠加：同一 Pod 同一方向被多条策略适用时，放行集合 = 各策略放行规则的**并集**，求值顺序无关。一条 Pod→Pod 连接要通，源端 egress 与目标端 ingress 必须同时放行；两侧都非隔离才默认通。

![[assets/network/diagram-networkpolicy-traffic-flow.svg]]

*图：连接判定流程——源端 egress 与目标端 ingress 双侧白名单都通过才放行，任一侧被隔离且无 allow 匹配即丢弃。*

> **关键点**：这是与防火墙 ACL「先匹配先生效、可显式 deny」模型的根本区别——API 没有 deny 规则，放行集合只会随策略增多而扩大，不会互相抵消。

### 规则内部 AND、规则之间 OR（peer 选择器四类）

一条 ingress/egress 规则内 `from`/`to` 数组是多个 peer 条目，条目间为 **OR**（任一匹配即放行）；`ports` 与 `from`/`to` 之间为 **AND**（同规则内端口与对端条件须同时满足）。peer 四类：

| peer 类型 | 匹配范围 | 典型用途 |
|-----------|---------|---------|
| `podSelector` | 仅**同命名空间**内带指定标签的 Pod | 放行本 NS 内特定应用 |
| `namespaceSelector` | 匹配标签的整个命名空间的**全部 Pod** | 放行整个命名空间 |
| 同一条目内 `namespaceSelector`+`podSelector` 并用 | AND：**特定命名空间内**特定标签的 Pod | 精确限定跨 NS 对端 |
| `ipBlock`（配 `except` 排除段） | CIDR 网段，多用于集群外 IP / 节点 IP | 外部网段白名单 |

![[assets/network/diagram-networkpolicy-selectors.svg]]

*图：peer 选择器决策——按对象位置与可标签化程度选择类型；同条目 = AND（收窄）、跨条目 = OR（放宽）。*

> **关键点（YAML 缩进陷阱）**：`namespaceSelector` 与 `podSelector` 写在**同一个** `from` 条目下 = AND（窄）；拆成**两个** `from` 条目 = OR（宽）。拿不准用 `kubectl describe networkpolicy` 看 API 实际解释。

### 默认策略配方（default-deny / allow-all）

改变命名空间默认行为靠四类模板：

| 配方 | YAML 形态 | 效果 |
|------|----------|------|
| default-deny-ingress | `podSelector: {}` + `policyTypes: [Ingress]` 无规则 | 阻断全部入站 |
| default-deny-egress | 同上，方向换 Egress | 阻断全部出站（⚠️ 连 DNS 一起拦） |
| default-deny-all | 两个方向都列 | 双向全拒 |
| allow-all | 空规则条目 `ingress: [- {}]` | 显式全放行，可覆盖同方向其他策略造成的隔离 |

> **关键点**：空 `podSelector={}` 选中命名空间**全部** Pod；空 `from`/`to` 条目 = 放行所有来源/目的地。allow-all 存在时任何其他策略都无法让该方向流量被拒（无 deny 规则）。

### 端口与协议边界（L4 only + endPort）

NetworkPolicy 定义在 **L4**：TCP、UDP、可选 SCTP（SCTP 需插件支持）。端口可用 `port` 单端口或 v1.25 起 stable 的 `port`+`endPort` 区间（两值须数字、`endPort >= port`、不能脱离 `port` 单独用）。

> **关键点**：deny-all 只保证拦 TCP/UDP/SCTP：**ARP、ICMP（ping）等协议行为未定义**，不同插件可能放行可能拦截——别把 ping 不通/通了当作策略正确性判据。

### 按命名空间与标签定位（无名称字段）

NetworkPolicy **无法直接用命名空间名字段或 Service 名称定位对象**：跨命名空间放行用 `namespaceSelector`+`matchLabels`/`matchExpressions`；要按名字选单一命名空间，用控制面自动打在每个 Namespace 上的不可变标签 `kubernetes.io/metadata.name`（值 = 命名空间名）。

> **关键点**：多命名空间 egress 场景：先 `kubectl label namespace` 打标，再在策略里 `matchExpressions` 用 `In` 操作符枚举。

### Pod 生命周期与策略生效时序

新策略下发到插件需要时间，且**无法从 K8s API 获知何时完成**：策略生效前新建的 Pod 可能裸奔启动（随后补上隔离）；策略生效后新建的被影响 Pod 会先隔离后启动，allow 规则可能在隔离规则之后才应用——最坏情况 Pod 刚启动时完全无网络。

> **关键点**：官方建议——需要保证启动即可达某目的地时，用 init container 轮询等待目的地可达，再启动业务容器。

### hostNetwork Pod 语义未定义

hostNetwork Pod 的 NetworkPolicy 行为未定义，只可能两种：插件能区分 hostNetwork 流量则照常应用策略；**不能区分（最常见实现）则忽略 hostNetwork Pod 的选择器匹配**，其流量等同节点流量按节点 IP 处理。

> **关键点**：既然 hostNetwork Pod 与节点同 IP，放行其流量通常改用 `ipBlock` 规则指向节点 IP 段；kube-system 里 hostNetwork 组件（kubelet 探活、CNI 等）因此常不受普通 Pod 策略管辖。

### API 能力边界（做不到的事）

官方明确 NetworkPolicy API 目前**无法**：显式 deny（只有默认拒绝+allow）、L7/TLS 相关（交给 Service Mesh/Ingress）、按节点 K8s 身份下发策略（只能用 CIDR）、按 Service 名放行、集群级默认策略（需第三方发行版）、策略命中/拒绝日志（交给 Hubble 等可观测层）、阻止 Pod 访问本机 localhost 或所在节点。

> **关键点**：设计网络隔离方案时先对照此清单：需要 L7 或日志审计就引入服务网格/可观测组件，别指望 NetworkPolicy 单点全包。

## 常见问题表

| # | 问题 | 原因 | 解决方案 | 官方参考 |
|---|------|------|---------|---------|
| 1 | 创建 NetworkPolicy 后流量完全不受影响，策略形同虚设 | NetworkPolicy 由 CNI 插件实现，API Server 不执行过滤；插件不支持/未启用（Flannel 无实现、GKE 需 `--enable-network-policy`、Calico 未部署 felix）时策略只是存了个对象 | 确认集群方案支持（Calico/Cilium/kube-router/Antrea）；GKE 建集群加 `--enable-network-policy`；kubeadm 按官方 provider 页部署；检查 `kubectl get pods -n kube-system` 中 Calico/Cilium 控制器 Pod 是否 Running | [prerequisites](https://kubernetes.io/docs/concepts/services-networking/network-policies/#prerequisites) |
| 2 | default-deny-egress 后 Pod 突然解析不了 DNS、出站全断 | 无规则的空 egress 策略会**连 DNS 流量一起拦掉**（官方 caution 明示） | 补一条显式放行集群 DNS 的策略：`namespaceSelector` 选 kube-system（或 `kubernetes.io/metadata.name=kube-system`），ports 放行 UDP 53 + TCP 53，再逐条加业务 allow | [default-deny-all-egress-traffic](https://kubernetes.io/docs/concepts/services-networking/network-policies/#default-deny-all-egress-traffic) |
| 3 | 默认拒绝后新建 Pod 启动时无网络或间歇连不上依赖 | 策略传播与 Pod 生命周期竞态：策略下发需时间，期间新建 Pod 可能裸奔/延迟被隔离；隔离规则先应用、allow 后应用，最坏启动瞬间无放行规则；API 无法获知插件处理完成时机 | 先 apply NetworkPolicy 再建工作负载；强依赖场景用 init container 轮询探测依赖可达（wget/nc 循环）后再启动业务容器 | [pod-lifecycle](https://kubernetes.io/docs/concepts/services-networking/network-policies/#pod-lifecycle) |
| 4 | 想按命名空间名/Service 名写策略，发现 API 不支持 | NetworkPolicy 没有 namespace 名称字段也没有 Service 选择器；跨 NS 只能 `namespaceSelector` 按标签选，Service 只能退化为按 Pod 标签选 | 给目标 NS 打业务标签后 namespaceSelector 选；按名字精确定位用自动标签 `kubernetes.io/metadata.name`；Service 背后 Pod 有稳定标签则直接 podSelector（官方明示的常见 workaround） | [targeting-a-namespace-by-its-name](https://kubernetes.io/docs/concepts/services-networking/network-policies/#targeting-a-namespace-by-its-name) |
| 5 | ingress 的 ipBlock 放行外部网段不生效或范围不对 | 集群入口（LoadBalancer/NodePort/云 LB）常改写源 IP，「改写发生在 NetworkPolicy 处理前还是后」官方定义为未定义行为——策略看到的源 IP 可能是真实客户端、LB IP 或节点 IP | 不要想当然用 ipBlock 做精确外部白名单；先在 Pod 内验证实际到达的源 IP（抓包）；需真实客户端 IP 用 `externalTrafficPolicy: Local`，或改用标签选择器 | [behavior-of-to-and-from-selectors](https://kubernetes.io/docs/concepts/services-networking/network-policies/#behavior-of-to-and-from-selectors) |
| 6 | 选择器把不该放行的 Pod 也放行了（范围意外扩大） | YAML 语义误解：同一 `from` 条目内 namespaceSelector 与 podSelector 并列是 **AND**；误写成两个独立 `from` 条目则是 **OR**（本地某标签 Pod 或任意 NS 全部 Pod），范围显著扩大 | 严格按缩进区分「同条目 = AND、跨条目 = OR」；`kubectl describe networkpolicy` 看 API 实际解释；用带/不带标签的测试 Pod 实测连通性（busybox + `wget --spider --timeout=1`） | [declare-network-policy](https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/) |
| 7 | hostNetwork Pod 不受策略管控，或策略误伤节点流量 | hostNetwork Pod 与节点同 IP，多数插件无法区分其流量，按节点流量处理：podSelector/namespaceSelector 不匹配它们；官方把该行为定义为未定义 | 接受平台现实：不用 Pod 选择器管辖 hostNetwork 组件；放行其流量用 ipBlock 指向节点 IP 段；把 kube-system 的 hostNetwork 组件（kubelet 探活、CNI、DNS）当节点面流量单独规划 | [networkpolicy-and-hostnetwork-pods](https://kubernetes.io/docs/concepts/services-networking/network-policies/#networkpolicy-and-hostnetwork-pods) |
| 8 | default-deny 后 ping（ICMP）仍通，怀疑策略失效 | NetworkPolicy 只保证 TCP/UDP/SCTP；deny-all 只保证拦这三种，ARP/ICMP 行为未定义，允许规则同理 | 连通性验收以 TCP/UDP 业务端口为准（wget/curl/nc），不要把 ICMP 通断当策略生效判据；需控 ICMP 依赖插件能力（如 Cilium Hubble/流规则）或接受差异 | [network-traffic-filtering](https://kubernetes.io/docs/concepts/services-networking/network-policies/#network-traffic-filtering) |
| 9 | port+endPort 区间策略只放行起始端口 | endPort v1.25 已 stable，但实际执行仍依赖插件支持；插件不支持时不报错、只按 port 单端口应用 | 确认插件版本支持 endPort（Calico/Cilium 新版本支持）；实测区间内多端口（如 32000 与 32768 都测）；只通起始端口即判定不支持，改用多条单端口规则或升级插件 | [targeting-a-range-of-ports](https://kubernetes.io/docs/concepts/services-networking/network-policies/#targeting-a-range-of-ports) |
| 10 | 改了策略或 Pod/Namespace 标签，既有连接没按新策略断开/放行 | 策略集合变化（增删策略、被选中对象标签变更）对「已建立连接」是否生效由插件实现决定，官方未定义 | 官方建议不要在会影响既有连接的方式下改策略/标签；确需立即收紧则接受「新连接按新策略、老连接由插件决定」，或滚动重启工作负载强制重连 | [impact-on-existing-connections](https://kubernetes.io/docs/concepts/services-networking/network-policies/#networkpolicys-impact-on-existing-connections) |

## 最佳实践

1. **默认拒绝 + 显式最小放行（Zero-trust 东西向基线）**：每个业务命名空间落地 default-deny-ingress 与 default-deny-egress（`podSelector: {}` + 对应 policyTypes、无规则），再逐条加业务必需 allow。API 只有 allow 无 deny，默认拒绝是唯一可靠的收敛手段；未纳管命名空间保持默认全放行。 — [官方 default policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/#default-policies)
2. **默认拒绝 egress 必须配套放行 DNS**：官方 caution：default-deny-egress 会连 DNS 一起阻断。落地顺序：deny-all → allow DNS（namespaceSelector 选 kube-system + UDP/TCP 53）→ allow 业务对端。 — [官方 default-deny-all-egress](https://kubernetes.io/docs/concepts/services-networking/network-policies/#default-deny-all-egress-traffic)
3. **用标签约定驱动策略，避免 IP 与名字硬编码**：Pod 用稳定角色标签（role/app/tier/access）作为选择器主语；跨命名空间统一打命名空间标签，按名字定位用自动标签 `kubernetes.io/metadata.name`；ipBlock 只用于集群外网段且理解源 IP 改写不确定性。标签化策略随 workload 扩缩自动覆盖新 Pod。 — [targeting by label](https://kubernetes.io/docs/concepts/services-networking/network-policies/#targeting-multiple-namespaces-by-label)
4. **先建策略、后建工作负载；启动强依赖用 init container 等待**：受策略影响的新 Pod 启动前即被隔离，allow 规则可能稍后才应用，最坏启动瞬间无网络。先 apply NetworkPolicy 再部署；必须启动即可达的依赖用 init container 探测就绪后再起主容器。 — [pod-lifecycle](https://kubernetes.io/docs/concepts/services-networking/network-policies/#pod-lifecycle)
5. **明确声明 policyTypes，不要依赖默认推断**：省略时 Ingress 恒被设置、Egress 仅当存在 egress 规则才被设置——语义绕且易错。显式写 `policyTypes: [Ingress, Egress]`（或只写要隔离的方向）让意图自文档化。 — [the-networkpolicy-resource](https://kubernetes.io/docs/concepts/services-networking/network-policies/#the-networkpolicy-resource)
6. **隔离系统命名空间要格外谨慎（kube-system/节点面）**：kube-system 大量组件是 hostNetwork 或节点面流量（kubelet 探活、CNI、CoreDNS），多数插件按节点流量处理、不受普通策略管辖；对系统命名空间一刀切 deny 可能弄断 DNS 与节点通信。先给系统面留白，再逐步收紧业务命名空间。 — [hostnetwork-pods](https://kubernetes.io/docs/concepts/services-networking/network-policies/#networkpolicy-and-hostnetwork-pods)
7. **策略变更走 kubectl describe 验证 + 标签化连通性实测**：`kubectl describe networkpolicy` 看 API 如何解释 from/to；用带/不带标签的 busybox 测试 Pod 执行 `wget --spider --timeout=1 <svc>`，无标签超时、有标签成功即策略按预期生效。隔离类变更先在小命名空间试点再推广。 — [declare-network-policy walkthrough](https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/)

## 排查命令

```bash
# 1. 确认集群 CNI 是否实现 NetworkPolicy（无实现则策略无效，问题 1）
kubectl get pods -n kube-system | grep -iE "calico|cilium|kube-router|antrea"
kubectl get networkpolicy -A                     # 列出全部策略对象
kubectl get netpol -n <ns>                       # 简写 netpol

# 2. 查看 API 对策略 from/to 条目的实际解释（缩进 AND/OR 是否如预期，问题 6）
kubectl describe networkpolicy <name> -n <ns>

# 3. 给命名空间打标签 / 按名字定位（问题 4）
kubectl label namespace <ns> team=platform      # 业务标签
kubectl get namespace <ns> -o jsonpath='{.metadata.labels.kubernetes\.io/metadata\.name}'

# 4. 连通性实测：带/不带标签的测试 Pod 互测（问题 6 验收）
kubectl run busybox-a --image=busybox --labels=access=allowed --rm -it -- sh
# 对端执行：wget --spider --timeout=1 http://<svc>.<ns>.svc  或  nc -zv <ip> <port>
# 无标签 Pod 超时、有标签 Pod 成功 = 策略按预期生效

# 5. 默认拒绝后 DNS 是否被误拦（问题 2）——在 Pod 内验证解析
kubectl exec <pod> -n <ns> -- nslookup kubernetes.default.svc.cluster.local

# 6. 观察数据面规则是否下发（Calico / Cilium 示例）
kubectl exec -n kube-system <calico-node-pod> -- calicoctl get policy   # Calico
kubectl exec -n kube-system <cilium-pod> -- cilium policy get           # Cilium
```

## 相关笔记

- [[k8s-cluster-networking]] — 全景视角：K8s 网络模型、Service/Ingress/DNS 基础
- [[k8s-network-dns-lb-cni]] — 控制面视角：DNS 解析链、负载均衡、CNI 插件链
- [[k8s-network-dataplane-tcpip]] — 数据面视角：包路径、MTU、conntrack、源 IP 改写（NetworkPolicy 的 ipBlock 源 IP 不确定性与 externalTrafficPolicy 在此展开）

官方参考：[Network Policies 概念](https://kubernetes.io/docs/concepts/services-networking/network-policies/) · [Declare Network Policy walkthrough](https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/) · [NetworkPolicy API 参考](https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/network-policy-v1/) · [Calico provider](https://kubernetes.io/docs/tasks/administer-cluster/network-policy-provider/calico-network-policy/) · [Cilium provider](https://kubernetes.io/docs/tasks/administer-cluster/network-policy-provider/cilium-network-policy/) · [kube-router provider](https://kubernetes.io/docs/tasks/administer-cluster/network-policy-provider/kube-router-network-policy/) · [NetworkPolicy recipes 集](https://github.com/ahmetb/kubernetes-network-policy-recipes)
