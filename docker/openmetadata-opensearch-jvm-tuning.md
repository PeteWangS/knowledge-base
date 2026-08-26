# OpenMetadata OpenSearch 频繁退出排查与 JVM 堆配置

> 实战环境：OpenMetadata 1.12.4 docker compose 部署（Harbor 镜像），OpenSearch 3.4.0
> 记录日期：2026-08

## 概述

OpenSearch 容器频繁退出（`Exited (127)`）的排查链路，以及调 JVM 堆时的**变量名陷阱**——`ES_JAVA_OPTS` 在 OpenSearch 镜像里不生效，必须用 `OPENSEARCH_JAVA_OPTS`。排查主线：退出元数据 → 日志 → 宿主资源 → 内核参数，按证据链定位而非猜。

## 现象

- `docker ps -a` 看到 opensearch 容器 `Exited (127)`
- 频繁挂，且 compose 里 opensearch 服务**没有 `restart: always`** → 挂了不会自动拉起，只能手动 up

## 排查命令（按顺序）

```bash
# ① 退出元数据（10 秒取证）
docker inspect openmetadata_opensearch --format \
'ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} RestartCount={{.RestartCount}} Error={{.State.Error}}'

# ② 退出前日志（核心证据）
docker logs --tail 100 openmetadata_opensearch

# ③ 宿主资源四项
free -h                        # 内存
df -h /var/lib/docker          # 磁盘
sysctl vm.max_map_count        # 内核参数（ES/OS 要求 262144）
dmesg | grep -iE 'oom|killed' | tail -20   # OOM killer 证据
```

日志特征对照：

| 日志特征 | 根因 | 解法 |
|---|---|---|
| `Native memory allocation (mmap) failed` / `insufficient memory for the Java Runtime` | 内存不足 | 降堆或加内存 |
| `max virtual memory areas vm.max_map_count [65530] is too low` | 内核参数 | `sysctl -w vm.max_map_count=262144` + 写 /etc/sysctl.conf |
| `no space left` / `device busy` | 磁盘满 | 清理/扩容 |
| 日志突然中断无异常 | OOM killer | 看 dmesg |

## 核心坑：ES_JAVA_OPTS vs OPENSEARCH_JAVA_OPTS

- **`ES_JAVA_OPTS` 是 Elasticsearch 的变量名；OpenSearch 认 `OPENSEARCH_JAVA_OPTS`**
- 现象：compose 里写 `ES_JAVA_OPTS=-Xms5G -Xmx5G`，环境变量传进去了（docker inspect 能看到），但 JVM 实际还是 `-Xms1g -Xmx1g`
- 为什么之前没暴露：镜像内 `jvm.options` 默认就是 `-Xms1g -Xmx1g`，和模板里写的 `-Xms1024m -Xmx1024m` 恰好相等，一直以为 ES_JAVA_OPTS 生效，其实一直是 jvm.options 默认值在起作用

正确写法：

```yaml
opensearch:
  image: image.ludp.lenovo.com/prd/openmetadata/opensearch:3.4.0
  restart: always
  environment:
    - OPENSEARCH_JAVA_OPTS=-Xms5G -Xmx5G      # ✅ 正确变量名
```

## JVM 参数叠加机制（docker top 会看到两套）

```
-Xms1g -Xmx1g     ← jvm.options 默认值（先加载）
-Xms5G -Xmx5G     ← OPENSEARCH_JAVA_OPTS 追加（后加载）
```

JVM 对重复的 `-Xms/-Xmx` **取最后一个**。所以 `docker top` 显示多套参数是正常的，不代表配置冲突。

## 验证配置生效（三层）

```bash
# ① 环境变量传进容器了（只证明"传了"）
docker inspect openmetadata_opensearch --format '{{range .Config.Env}}{{println .}}{{end}}' | grep JAVA_OPTS

# ② JVM 命令行（docker top 全量，取最后两个）
docker top openmetadata_opensearch | grep -oE '\-Xm[sx][0-9a-zA-Z]*' | tail -2

# ③ JVM 运行时（最权威，jcmd 查活进程）
docker exec openmetadata_opensearch bash -c 'PID=$(jps | awk "/OpenSearch/{print \$1}"); jcmd $PID VM.flags | grep -oE "\-XX:(Initial|Max)HeapSize=[0-9]+"'
# 5G = 5368709120 字节；1G = 1073741824
```

**判定标准**：①② 只证明配置传了，③ 证明 JVM 真按目标值跑——以 ③ 为准。看原始字节数对照 `unit` 列换算。

## JRE 容器没有 jps/jstat/jcmd

openmetadata-server 镜像用 `openjdk21-jre`（JRE 不带 JDK 诊断工具），jps/jstat/jcmd 是 JDK 的。opensearch 镜像带完整 JDK 所以能用。JRE 容器的替代手段：

```bash
# ① 看 GC 日志文件（OpenMetadata 默认输出 openmetadata-gc.log）
docker exec openmetadata_server tail -50 /opt/openmetadata/logs/openmetadata-gc.log

# ② 宿主机视角（Java 进程 PID 在宿主 /proc 可见）
docker top openmetadata_server | head -5
cat /proc/<PID>/status | grep -E 'VmRSS|VmHWM'

# ③ docker stats 采样内存行为（波动=GC 正常；纹丝不动顶满=堆压力）
for i in 1 2 3 4 5; do docker stats --no-stream --format '{{.MemUsage}}' openmetadata_server; sleep 2; done
```

## GC 日志速读

```
Pause Young (Normal) (G1 Evacuation Pause) 8204M->3118M(8792M) 19.455ms
```

- `Pause Young` = 年轻代 GC，停顿小（几十 ms）不是慢的主因
- **出现 `Pause Full` 才是大问题**（Full GC 停顿秒级）
- `Old regions: 369->369` 稳定 = 无内存泄漏（持续上涨才危险）
- GC 编号大（如 86445）说明累计 GC 多，曾有过高分配率时期（采集/高负载）

## 常见问题表

| 问题 | 原因 | 解决 |
|---|---|---|
| Exited (127) | 入口脚本命令失败 / 资源被杀 | 按排查命令 ①②③ 定位 |
| 堆改了不生效 | 用了 ES_JAVA_OPTS | 换 OPENSEARCH_JAVA_OPTS |
| docker top 两套 -Xms | 参数叠加，正常现象 | JVM 取最后一个，以 jcmd 为准 |
| jps/jstat 不存在 | JRE 容器无 JDK 工具 | 看 GC 日志文件 / 宿主 /proc |
| 容器挂了自己不拉起 | compose 无 restart: always | 加 `restart: always` |

## 最佳实践

1. **调堆用对变量名**：OpenSearch = `OPENSEARCH_JAVA_OPTS`，Elasticsearch = `ES_JAVA_OPTS`，别混用
2. **堆大小与宿主内存匹配**：5G 堆 + 堆外索引缓存，宿主可用内存至少留 8G+，改完 `free -h` 盯一下
3. **以运行时值为准**：docker top 看到多套参数别慌，jcmd VM.flags 是唯一权威
4. **compose 里给 opensearch 配 `restart: always`**，避免挂了无人拉起
5. **jvm.options 默认 1g 会掩盖变量不生效问题**，调堆后必须验证（jcmd），不要想当然

## 相关笔记

- [[openmetadata-containerd-deployment]] — OpenMetadata containerd 部署实战（含熵池耗尽等部署期坑）
- [[openmetadata-postgres-performance-tuning]] — OpenMetadata Web 慢排查与 PostgreSQL 参数调优
