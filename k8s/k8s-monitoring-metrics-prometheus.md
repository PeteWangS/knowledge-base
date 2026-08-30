---
created: 2026-08-30
topic: K8s实战
subtopic: K8s 监控体系深潜：Metrics API 资源指标管道与 Prometheus/Grafana 告警闭环
source: https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/ https://prometheus.io/docs/concepts/metric_types/ https://github.com/prometheus-operator/kube-prometheus
tags: [kubernetes, monitoring, prometheus, grafana, metrics-api, metrics-server, alertmanager, kube-prometheus, kube-state-metrics]
---

# K8s 监控体系深潜：Metrics API 资源指标管道与 Prometheus/Grafana 告警闭环

## 概述

Kubernetes 集群监控按官方模型分为**两条管道**：资源指标管道（resource metrics pipeline）由 metrics-server 实现，通过 Metrics API（`metrics.k8s.io/v1`，官方文档标注 v1.37 stable）把节点/Pod 的 CPU、内存用量喂给 HPA、VPA 与 `kubectl top`；完整指标管道（full metrics pipeline）由 Prometheus 生态承担，采集、存储、查询、告警全链路。官方推荐的快速全栈方案是 **kube-prometheus**：基于 Prometheus Operator 的 jsonnet 库，内置 HA Prometheus + HA Alertmanager + node-exporter + blackbox-exporter + prometheus-adapter + kube-state-metrics + Grafana，并预置 dashboards 与告警规则。

监控体系运维的核心素养：指标类型语义（Counter/Gauge/Histogram/Summary）、命名与记录规则规范、对症状告警的哲学、Prometheus 安全模型，以及 kube-prometheus 的典型排错（kubelet 401/403、kube-proxy 绑定地址、与 metrics-server 的 APIService 冲突）。

## 架构图

![[assets/k8s/diagram-k8s-monitoring-arch.svg]]

*图：双层指标管道架构——资源指标管道（kubelet → metrics-server → Metrics API → HPA/kubectl top）与完整指标管道（导出器 → Prometheus → recording rules → Grafana/Alertmanager），kube-prometheus 以 prometheus-adapter 兼作资源指标 API 提供者*

## 核心概念

### Metrics API 与资源指标语义

`metrics.k8s.io/v1` 提供 NodeMetrics/PodMetrics，响应含 **timestamp 与 window 字段**。CPU 是窗口内平均核心数（对内核累计 CPU 计数器取 rate，1 cpu = 1 vCPU/核心或 1 超线程），Memory 是采集瞬间的 **working set**（字节，含部分无法回收的 file-backed 缓存，由运行时启发式计算）。

> 关键点：CPU 用 rate 语义（窗口平均）、内存用即时 working set；官方文档标注 v1.37 stable。

### metrics-server 工作方式

metrics-server 通过 Kubernetes API 追踪节点与 Pod，逐个节点走 HTTP 查询 kubelet（v0.6.0+ 用 `/metrics/resource` 端点），内部构建 pod 元数据视图并缓存 pod 健康状态，HPA 查询时按 label selector 识别目标 Pod 集合。

> 关键点：HPA 依赖它按 selector 匹配 Pod；不存历史数据，只服务实时资源查询。

### 指标类型：Counter 与 Gauge

- **Counter**：单调递增累计值，只能增加或重启归零（请求数、错误数、完成任务数）
- **Gauge**：可任意升降（当前进程数、内存用量、并发请求数）

> 关键点：会下降的数值绝不能用 Counter，否则语义错误（`rate()` 会算出负值或乱跳）。

### 指标类型：Histogram（classic vs native）

Histogram 把观测值按可配置 bucket 计数，暴露 `_bucket{le=上界}`（累积计数）+ `_sum` + `_count` 三条序列。**Native histogram** 是复合样本：动态 bucket 集合 + 计数 + 和，无需配置 bucket 边界、分辨率更高、网络传输原子（classic 的多条序列经 remote write 可能部分传输）、bucket 变更后仍可聚合；Go/Java 客户端支持。classic 不同 bucket 边界不可聚合；NHCB（自定义边界 native）是折中。quantile 用 `histogram_quantile()` 计算（classic/native 语法略异），也适合算 Apdex。

> 关键点：优先 native histogram；histogram 可跨实例聚合，Summary 不行。

### 指标类型：Summary

Summary 在客户端计算流式 φ-quantiles（滑动时间窗口），暴露 `{quantile=φ}` + `_sum` + `_count`。Prometheus v3.0 起 quantile 标签值按 OpenMetrics Canonical Numbers 归一化。

> 关键点：quantile 不可跨实例聚合（客户端算的），需要聚合 percentile 必须用 Histogram。

![[assets/k8s/diagram-k8s-metric-type-choice.svg]]

*图：指标类型选型决策流——会下降选 Gauge，单调累计按「是否需要聚合 percentile → 是否客户端算分位数」分叉到 Histogram / Summary / Counter*

### 指标命名规范

指标名 = 应用前缀（命名空间）+ 单单位 + 基础单位后缀（`seconds`/`bytes`/`ratio` 而非 ms/MB/%），计数类加 `_total`；用 labels 区分被测对象特征（operation/stage），不要把 label 名塞进指标名；避免高基数标签（user_id、email 等无界集合）。

> 关键点：单位与类型后缀入名是 Prometheus 强烈建议（YAML 排错可读性、防系列碰撞），与 OpenTelemetry 做法不同。

### Recording rules 规范

规则命名通用形式 `level:metric:operations`（level=聚合层级与剩余标签，metric=原指标名，rate/irate 时去掉 `_total`，operations 按最新在前列出，`_sum` 在有其他操作时省略，除法用 `_per_` + ratio）。聚合 ratio 必须**分子分母分别聚合再除**，不能对平均值再取平均；聚合用 `without` 子句显式列出被聚合掉的标签。

> 关键点：示例链 `instance_path:requests:rate5m` → `path:requests:rate5m` → `job:request_failures_per_requests:ratio_rate5m`。

### 告警哲学

尽可能少告警：对**症状**告警（与终端用户痛感相关）而非穷举所有可能成因；告警链接到相关 console；容忍小抖动（留 slack）；在线服务只在栈的一个点 page 延迟，错误率 page 用户可见错误；批任务失败容忍至少 2 次完整运行（4 小时跑 1 小时的作业阈值约 10 小时）；容量类告警防未来 outage；必须有 **metamonitoring**（监控监控自身，黑盒测试链路优于逐组件告警）。

![[assets/k8s/diagram-k8s-alerting-flow.svg]]

*图：告警闭环流程——采集 → 规则评估（expr + for）→ Alertmanager 去重/分组/路由 → 按 severity 分派（critical page 值班 / warning 邮件）→ 人工处理 → resolved 恢复通知，静默/抑制在 Alertmanager 侧配置*

### Prometheus 安全模型

默认假设：任何能访问 HTTP 端点的人可读全部时序数据；仅可信用户可改配置。`--web.enable-admin-api` 与 `--web.enable-lifecycle` 默认关闭（`/api/*/admin/`、`/-/reload`、`/-/quit`）。Alertmanager 无认证时 `/api/v2` 带 `Access-Control-Allow-Origin: *`（浏览器可跨站访问）。Pushgateway 常配 `honor_labels`，任何人可伪造任意时序。TLS 1.2 起、basic auth 密码 bcrypt 存储；管理/变更端点无内置 CSRF 保护，反代应拦截；Grafana dashboard 权限 ≠ 数据源权限。PromQL 拼接不可信输入需转义防注入。

> 关键点：admin/lifecycle 默认关；Alertmanager 必须配认证；反代拦 `/api/*/admin/` 防 CSRF。

### kube-prometheus 栈

jsonnet 库 + 编译产物 `manifests/`。组件：Prometheus Operator、HA Prometheus、HA Alertmanager、node-exporter、blackbox-exporter、**prometheus-adapter**（资源指标 API 提供者）、kube-state-metrics、Grafana。前置条件：kubelet 开 `--authentication-token-webhook=true` 与 `--authorization-mode=Webhook`（用 ServiceAccount token 细粒度授权，而非给 Prometheus 客户端证书全权）。安装用 `kubectl apply --server-side -f manifests/setup`（CRD）→ `kubectl wait CRD Established` → apply `manifests/`。

> 关键点：先 setup（CRD）后 main；kubelet 双 flag 是抓取成功的前提。

### Grafana 接入

默认 `http://localhost:3000`，admin/admin；数据源选 Prometheus 填 URL 并 Save & Test；查询用 PromQL，Legend format 用 `{{method}} - {{status}}` 模板；Grafana 7.2+ 推荐 `$__rate_interval` 变量用于 rate/increase；导入 grafana.com dashboard 需手改 JSON 的 datasource 字段匹配数据源名。

> 关键点：`$__rate_interval` 替代固定 5m；导入仪表盘需改 datasource 引用。

## 常见问题表

| 问题 | 原因 | 解决 | 官方出处 |
|------|------|------|----------|
| Prometheus /targets 页 kubelet job 报 401 Unauthorized | kubelet 未启用 token 认证：缺 `--authentication-token-webhook=true`（或 `authentication.webhook.enabled=true`） | 在所有 kubelet 配置中启用该 flag（kubeadm 集群改 `/var/lib/kubelet/config.yaml` 的 `authentication.webhook.enabled` 并重启 kubelet）；部分云厂商/网络插件有专属支持页（GKE/EKS/Weave Net） | kube-prometheus docs/troubleshooting.md — Authentication problem |
| Prometheus /targets 页 kubelet job 报 403 | kubelet 未启用 Webhook 授权：缺 `--authorization-mode=Webhook`，Prometheus 请求无法通过 RBAC 校验 | 启用 `--authorization-mode=Webhook`（或 `authorization.mode=Webhook`），让 kubelet 向 API server 做 RBAC 授权查询；注意 **401 是认证问题、403 是授权问题**，按错误码区分排查 | kube-prometheus docs/troubleshooting.md — Authorization problem |
| kube-proxy 指标抓取不到（target UP 但无数据/连接拒绝） | kubeadm 默认把 kube-proxy 指标监听在 127.0.0.1，Prometheus 无法跨节点访问 | 集群初始化前在 kubeadm 配置的 KubeProxyConfiguration 里设 `metricsBindAddress: 0.0.0.0:10249`；已运行集群改 kube-system 的 kube-proxy ConfigMap 同名字段后 `kubectl -n kube-system rollout restart daemonset kube-proxy` | kube-prometheus docs/troubleshooting.md — Error retrieving kube-proxy metrics |
| 安装 kube-prometheus 报 `apiservices.apiregistration.k8s.io v1beta1.metrics.k8s.io AlreadyExists` | 集群已有 metrics-server 且 prometheus-adapter 也注册同一 APIService——资源指标 API 一个集群只能有一个 provider | 二选一：卸载既有 metrics-server（`helm -n kube-system uninstall metrics-server`，或删 deployment/service/APIService/相关 clusterrole 与 binding）；或改用 metrics-server 作为资源指标 API（`values.common.resourceMetricsAPI: metrics-server`）。注意卸载 kube-prometheus 会连带删除共享的 `v1beta1.metrics.k8s.io` APIService，破坏依赖它的 kubectl top 与 HPA | kube-prometheus docs/troubleshooting.md — Conflict with an existing metrics-server |
| kube-state-metrics 资源用量高/OOM | namespace 数量多等环境因素驱动内存需求上升 | 其资源由 addon-resizer 自动调节，可在 jsonnet 配置覆盖参数（默认 baseCPU 100m + cpuPerNode 2m、baseMemory 150Mi + memoryPerNode 30Mi） | kube-prometheus docs/troubleshooting.md — kube-state-metrics resource usage |
| 把可增可减的数值（如当前进程数）用 Counter 暴露 | Counter 语义是单调递增累计值，暴露可下降数值会破坏 rate() 等计算 | 改用 Gauge 表示可任意升降的测量值（温度、内存用量、并发请求数）；Counter 只用于请求数/错误数/完成任务数等单调累计场景 | prometheus/docs concepts/metric_types.md — Counter/Gauge |
| 对 Summary 的 quantile 做跨实例聚合得到错误百分位 | Summary 的 φ-quantile 在客户端按滑动窗口计算，聚合时无法正确合并 | 需要聚合 percentile 时改用 Histogram（bucket 是累计计数，可聚合后经 `histogram_quantile()` 计算）；客户端库支持时优先 native histogram（Go/Java），分辨率更高且 bucket 变更后仍可聚合 | prometheus/docs concepts/metric_types.md — Histogram/Summary |
| recording rule 对 ratio 类指标取平均（average of averages） | 先算各实例 ratio 再聚合平均在统计上无效（分子分母未分开聚合） | 先分别聚合 numerator 与 denominator，最后再除；Summary 的 `_sum/_count` 求平均观测值时保留原名、把 rate 换成 mean（如 `instance_path:request_latency_seconds:mean5m`）；聚合务必用 `without` 子句显式列出被聚合掉的标签 | prometheus/docs practices/rules.md — Aggregation |
| 把 user_id/email 等高基数维度放进 label 导致时序爆炸 | 每个唯一 label 组合都是一条独立时间序列，无界取值集合使存储与查询成本失控 | 标签只用于区分被测对象的有界特征（operation/stage 等）；不要在指标名里重复 label 名（聚合掉后产生冗余与歧义） | prometheus/docs practices/naming.md — Labels |

## 最佳实践

1. **kubelet 前置条件：token 认证 + Webhook 授权** —— 监控栈访问 kubelet 必须开 `--authentication-token-webhook=true` 与 `--authorization-mode=Webhook`，用 ServiceAccount token 做细粒度 RBAC 授权，而不是给 Prometheus 客户端证书（证书 = kubelet 全权访问）；这是 kube-prometheus 抓取 kubelet 指标的前提。（kube-prometheus README — Prerequisites）
2. **指标命名：前缀 + 基础单位 + 类型后缀** —— 指标名带应用前缀（`prometheus_notifications_total`、`node_memory_usage_bytes`）、只含单一单位、用基础单位（seconds/bytes/ratio，不用 ms/MB/%）、计数加 `_total`；单位与类型写进名字便于排错时直接读 YAML 里的 PromQL 表达式。（prometheus/docs practices/naming.md）
3. **Recording rules 命名与聚合纪律** —— 统一 `level:metric:operations` 命名；rate/irate 时去掉 `_total`；聚合 ratio 分子分母分开聚再除；用 `without(instance, path)` 保留 job 等维度；无聚合时输出层级必须与输入一致，否则规则有误。（prometheus/docs practices/rules.md）
4. **对症状告警、保持告警数最小** —— 对终端用户痛感相关的症状告警而非穷举成因；延迟只在栈的一个点 page；批任务失败至少容忍 2 次完整运行；容量临近人工干预；配 metamonitoring 保监控自身可用（黑盒链路测试优于逐组件告警）。（prometheus/docs practices/alerting.md）
5. **优先 native histogram** —— Go/Java 客户端支持时用 native histogram：无需配置 bucket 边界、分辨率更高、remote write 原子传输（classic 多条序列可能部分传输）、bucket 变更后可聚合；不得已用 classic 时可用 NHCB 折中。（prometheus/docs concepts/metric_types.md）
6. **Prometheus 安全基线** —— 保持 `--web.enable-admin-api` 与 `--web.enable-lifecycle` 关闭；反代拦截 `/api/*/admin/` 与 `/-/reload` 防 CSRF（无内置防护）；Alertmanager 无认证时 `/api/v2` CORS 全开必须配认证；Pushgateway 配 honor_labels 时任何人可伪造时序；Grafana 仪表盘权限不是数据源权限。（prometheus/docs operating/security.md）
7. **Grafana 查询用 `$__rate_interval`** —— Grafana 7.2+ 在 rate/increase 中推荐 `$__rate_interval` 变量（按面板时间范围与步长自动计算），避免固定 5m 带来的采样对齐问题；导入 grafana.com 共享 dashboard 需手动修正 JSON 中的 datasource 字段。（prometheus/docs visualization/grafana.md）
8. **kube-prometheus 以库方式使用 + 分步安装** —— 项目定位是 jsonnet 库而非 fork 副本；quickstart 先 `kubectl apply --server-side -f manifests/setup`（namespace + CRD），`kubectl wait CRD Established` 后再 apply `manifests/`，避免 CRD 竞态；生产部署前必读 Customizing kube-prometheus、access-ui 与 troubleshooting。（kube-prometheus README — Quickstart）

## 排查命令

```bash
# 1. 验证资源指标管道（Metrics API 是否可用）
kubectl top nodes                      # 节点 CPU/内存
kubectl top pods -A                    # 全命名空间 Pod
kubectl get --raw /apis/metrics.k8s.io/v1/nodes | jq .    # 直接查 Metrics API
kubectl get apiservices | grep -i metrics   # APIService 状态（Available=True 才正常）

# 2. 验证 kubelet 抓取（401/403 排查）
kubectl -n monitoring get secret prometheus-k8s-token-XXXX -o jsonpath='{.data.token}' | base64 -d
# 用该 token 请求 kubelet 资源端点（先 kubectl proxy 或直连节点）：
curl -k -H "Authorization: Bearer $TOKEN" https://<node-ip>:10250/metrics/resource

# 3. kube-prometheus 组件与日志
kubectl -n monitoring get pods -o wide          # 全组件状态
kubectl -n monitoring logs -l app=prometheus -c prometheus --tail=200
kubectl -n monitoring port-forward svc/grafana 3000:3000   # 浏览器开 Grafana
kubectl -n monitoring port-forward svc/prometheus-operated 9090:9090   # Prometheus UI /targets

# 4. 规则检查（本地用 promtool，无需连集群）
promtool check rules rules.yaml        # 语法检查
promtool test rules tests.yaml         # 单元测试
curl -s localhost:9090/api/v1/rules    # 运行时规则状态（含 evaluation 错误）

# 5. kube-proxy 指标修复（已运行集群）
kubectl -n kube-system edit cm kube-proxy   # metricsBindAddress: 0.0.0.0:10249
kubectl -n kube-system rollout restart daemonset kube-proxy

# 6. metrics-server 与 prometheus-adapter 冲突处理
helm -n kube-system uninstall metrics-server   # 卸载其一，资源指标 API 只能有一个 provider
kubectl get apiservice v1beta1.metrics.k8s.io -o yaml   # 确认 provider 身份

# 7. 指标语义自检
# 会下降的指标确认用 Gauge；聚合 percentile 用 histogram_quantile() 而非对 Summary 求均值
```

## 相关笔记

- [[k8s-cluster-troubleshooting]] — 集群排错方法论（监控是排错的眼睛）
- [[k8s-debug-toolchain-deepdive]] — kubectl debug/crictl 工具链（与监控互补的运行时排查）
- [[k8s-pod-scheduling]] — HPA/VPA 依赖 Metrics API 做弹性伸缩（资源指标管道的消费端）
- [[k8s-core-objects-design]] — APIService 聚合层原理（Metrics API 扩展 API server 的机制基础）
- [[k8s-cluster-ops-deepdive]] — etcd/drain 等运维操作（与告警闭环联动）
