# OpenMetadata Web 慢排查与 PostgreSQL 参数调优

> 实战环境：OpenMetadata 1.12.4 docker compose（Harbor 镜像），PostgreSQL 15 容器
> 记录日期：2026-08

## 概述

OpenMetadata Web 页面访问极慢（`/api/v1/system/version` 响应 96 秒）的完整排查链路，最终定位为 **OpenLineage 血缘上报风暴 + PostgreSQL 全默认参数**。本文记录"逐层排除法"方法论、PG15 容器参数调优（compose command 方式）、索引与 autovacuum 补充。

## 现象

- Web 页面访问特别慢，轻接口 `/api/v1/system/version` 都要 96 秒
- server 容器 NET I/O 巨大（326GB/256GB）
- postgres 有大量活跃查询

## 排查链路（逐层排除法，按证据说话）

| 层 | 检查 | 结论 |
|---|---|---|
| ① JVM 内存 | GC 日志（openmetadata-gc.log）：全部 Pause Young、停顿 20ms、Old 稳定、无 Pause Full | ✅ 排除内存/GC |
| ② opensearch | `curl localhost:9210/_cluster/health` 9ms、CPU 1.2%（yellow + 大量 unassigned 是单节点副本分片常态） | ✅ 排除 |
| ③ ingestion | 日志只有 Airflow scheduler 心跳，无采集任务 | ✅ 排除 |
| ④ server 日志 | **`Slow request detected - POST /api/v1/openlineage/lineage, total: 33737ms, internal: 27594ms (81%), dbOps: 156`** | ❗ **根因入口** |
| ⑤ postgres | postgresql.conf 全部 PG15 出厂默认值（shared_buffers=128MB 等） | ❗ **放大因素** |

**根因**：hive/spark/trino 的 OpenLineage 血缘上报接口（`POST /api/v1/openlineage/lineage`）被高频调用，单次请求内部做 **156 次 DB 操作**（entity_relationship 表读写），server 线程池被占满 → 所有 API 排队。postgres 全默认参数让每次 dbOps 更慢，双重放大。

关键证据特征：

- 慢请求日志：`db: 6143ms (18%), internal: 27594ms (81%), dbOps: 156`
- 线程编号巨大（如 dw-3793398）→ 海量请求已处理
- `Connection reset by peer` 反复 → 客户端等不及断开重试，形成恶性循环
- postgres 活跃查询：`EntityRelationshipDAO.insert` + `SystemDAO.getBrokenRelationFromParentToChild`

## PostgreSQL 15 容器参数调优

### 修改方式：compose 的 command 传参（推荐，持久且随部署走）

```yaml
postgresql:
  image: image.ludp.lenovo.com/prd/openmetadata/postgres:15
  command: "--work_mem=64MB --shared_buffers=4GB --effective_cache_size=24GB \
--maintenance_work_mem=1GB --max_connections=500 --max_wal_size=4GB \
--min_wal_size=1GB --autovacuum_max_workers=5 \
--log_min_duration_statement=1000 --log_line_prefix='%t [%p] %q%u@%d '"
```

### 参数对照表（PG15 默认 → 推荐）

| 参数 | 默认 | 推荐 | 说明 |
|---|---|---|---|
| shared_buffers | 128MB | 4GB | 缓存热表（entity_relationship） |
| effective_cache_size | 4GB | 24GB | 让优化器敢走索引 |
| work_mem | 4MB | 64MB | 排序/哈希 |
| maintenance_work_mem | 64MB | 1GB | 建索引/vacuum |
| max_connections | 100 | 500 | server 并发 + 运维余量 |
| max_wal_size | 1GB | 4GB | 大量 insert 减少 checkpoint 频率 |
| autovacuum_max_workers | 3 | 5 | 大表 insert 快，vacuum 跟上 |

### 验证生效

```bash
docker compose up -d postgresql    # 数据卷不动，安全重建
docker compose exec postgresql psql -U postgres -c \
"SELECT name, setting FROM pg_settings WHERE name IN ('shared_buffers','work_mem','max_connections','effective_cache_size','max_wal_size');"
```

**⚠️ pg_settings.setting 是原始值，对照 unit 列换算**：

| 参数 | unit | 目标原始值 |
|---|---|---|
| shared_buffers | 8kB | 524288（=4GB） |
| effective_cache_size | 8kB | 3145728（=24GB） |
| work_mem | kB | 65536（=64MB） |
| max_wal_size | MB | 4096（=4GB） |
| max_connections | 无 | 500 |

**⚠️ postgres 容器内 postgresql.conf 在数据卷里（PGDATA），改文件也持久，但推荐 compose command**——声明式、一眼可见、部署即配置，`pg_settings.source` 列显示 `command line` 可确认来源。

## 索引与 autovacuum 补充

```bash
# 大表加索引必须 CONCURRENTLY（不锁表）
docker compose exec postgresql psql -U postgres -d openmetadata_db -c \
"CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_er_from_id ON entity_relationship(fromId);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_er_to_id ON entity_relationship(toId);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_er_type ON entity_relationship(type);"

# 重点表单独加强 autovacuum（表级 scale_factor 覆盖全局）
docker compose exec postgresql psql -U postgres -d openmetadata_db -c \
"ALTER TABLE entity_relationship SET (autovacuum_vacuum_scale_factor=0.05, autovacuum_vacuum_insert_scale_factor=0.05, autovacuum_analyze_scale_factor=0.05);"

# 检查表膨胀（大量 insert 后必查）
docker compose exec postgresql psql -U postgres -d openmetadata_db -c \
"SELECT relname, n_live_tup, n_dead_tup, last_autovacuum FROM pg_stat_user_tables WHERE relname='entity_relationship';"
```

## 慢查询日志（调优期必须开）

`log_min_duration_statement=1000`（>1s 记录）+ `log_line_prefix` 带时间/用户，日志在：

```bash
docker compose exec postgresql sh -c 'tail -20 /var/lib/postgresql/data/log/*.log'
```

## 常见问题表

| 问题 | 原因 | 解决 |
|---|---|---|
| 轻接口 96 秒 | openlineage 风暴占满线程池 | 停上报源止血 → DB 调优 → 业务侧降频/批量 |
| 改参数后不生效 | 容器没重建 / command 缩进错 | 确认 Uptime、对照 pg_settings 验证 |
| 原始值看不懂 | pg_settings.setting 无单位 | 对照 unit 列换算 |
| 索引查询为空 | 表名/schema 不对 | `SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE '%entity%';` |
| opensearch yellow | 单节点副本分片无法分配 | 非故障，unassigned 是常态 |

## 最佳实践

1. **逐层排除**：GC → opensearch → ingestion → server 日志（Slow request 是关键入口）→ DB，每层一条命令出结论，别猜
2. **Slow request 日志是 OpenMetadata 排障金矿**：`db%/search%/internal%` 直接告诉你慢在哪一段
3. **改参数必验证**：pg_settings（含 source 列）确认来源，JVM 用 jcmd 确认运行时值
4. **业务侧配合**：OpenLineage 客户端调大超时（消除重试风暴）+ 批量上报（减少请求数），比 server 硬扛有效
5. **治本靠升级**：OpenMetadata 新版本 openlineage 写入有批处理优化，1.12.4 的实现就是每请求 156 dbOps

## 相关笔记

- [[openmetadata-opensearch-jvm-tuning]] — OpenSearch 频繁退出排查与 JVM 堆配置（同栈运维笔记）
- [[openmetadata-containerd-deployment]] — OpenMetadata 部署实战（Harbor 镜像、迁移、踩坑）
