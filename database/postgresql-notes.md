# PostgreSQL 笔记

## 常用命令

```sql
-- 查看连接数
SELECT count(*) FROM pg_stat_activity;

-- 查看表大小
SELECT pg_size_pretty(pg_total_relation_size('table_name'));

-- 查看数据库大小
SELECT pg_database_size('database_name');
```

## 维护技巧

- 定期 `VACUUM ANALYZE` 防止事务回卷
- `pg_stat_statements` 定位慢查询
- 流复制监控 `pg_stat_replication`

## 相关笔记

- [[linux/postgresql-install]] — 安装指南
