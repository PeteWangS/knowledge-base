---
created: 2026-08-13
tags: [openmetadata, containerd, nerdctl, harbor, docker, deployment]
---

# OpenMetadata 1.12.4 containerd 部署实战（Harbor 镜像源）

> 场景：TST 环境，目标机为纯 containerd（无 docker daemon），用 `nerdctl compose` 从私有 Harbor 拉取并启动 OpenMetadata 全栈。本文记录完整操作步骤、全部踩坑与 containerd 环境的特殊差异。

## 概述

| 项 | 值 |
|---|---|
| 应用 | OpenMetadata 1.12.4（server + ingestion/airflow + postgres + opensearch） |
| 目标机 | node197-1-row-tst（Rocky 8 / x86_64，containerd + nerdctl） |
| 镜像仓库 | `registry-earth-reston-tst.xcloud.lenovo.com`（Harbor，项目 `ludprowtst`，私有） |
| 访问入口 | `https://openmetadata-row.tst-ludp.lenovo.com` → 10.63.105.10（nginx 反代 443 → 8585） |
| 初始账号 | `admin@open-metadata.org / admin`（登录后尽快改密） |

### Harbor 镜像清单（compose 5 个服务对应关系）

| compose 服务 | Harbor 镜像 | 备注 |
|---|---|---|
| postgresql | `ludprowtst/openmetadata/postgres:15` | 打包机构建（内置建库脚本） |
| opensearch | `ludprowtst/openmetadata/opensearch:3.4.0` | |
| execute-migrate-all | `ludprowtst/openmetadata/execute-migrate-all:1.12.4` | 一次性迁移容器 |
| openmetadata-server | `ludprowtst/openmetadata/openmetadata-server:1.12.4-D20260812-195408` | |
| ingestion | `ludprowtst/openmetadata/ingestion:1.12.4` | airflow |

## 架构图

![[assets/docker/diagram-openmetadata-containerd-arch.svg]]

*图：OpenMetadata containerd 部署拓扑（Harbor → nerdctl compose → 5 容器 → nginx 反代）*

## 一、登录 Harbor（containerd 环境）

containerd 对私有仓库的认证/TLS 与 docker 不同，**必须**配 CA 证书 + hosts.toml：

```bash
# 1. 下载仓库证书链（叶子+中间CA+根，共 3 张）
mkdir -p /etc/containerd/certs.d/registry-earth-reston-tst.xcloud.lenovo.com
echo | openssl s_client -showcerts \
  -connect registry-earth-reston-tst.xcloud.lenovo.com:443 \
  -servername registry-earth-reston-tst.xcloud.lenovo.com 2>/dev/null \
  | sed -n '/BEGIN CERTIFICATE/,/END CERTIFICATE/p' \
  > /etc/containerd/certs.d/registry-earth-reston-tst.xcloud.lenovo.com/ca.crt

# 2. hosts.toml 指定使用该 CA
cat > /etc/containerd/certs.d/registry-earth-reston-tst.xcloud.lenovo.com/hosts.toml <<'EOF'
server = "https://registry-earth-reston-tst.xcloud.lenovo.com"

[host."https://registry-earth-reston-tst.xcloud.lenovo.com"]
  ca = ["/etc/containerd/certs.d/registry-earth-reston-tst.xcloud.lenovo.com/ca.crt"]
EOF

# 3. 双保险：系统信任库 + 重启 containerd
cp /etc/containerd/certs.d/registry-earth-reston-tst.xcloud.lenovo.com/ca.crt /usr/local/share/ca-certificates/lenovo-registry-tst.crt
update-ca-certificates
systemctl restart containerd

# 4. 登录 + 试拉
nerdctl login registry-earth-reston-tst.xcloud.lenovo.com   # ludpadmin / <密码>
nerdctl pull registry-earth-reston-tst.xcloud.lenovo.com/ludprowtst/openmetadata/postgres:15
```

> 探测仓库连通性：`curl -sk https://<仓库>/v2/` 返回 401 = 仓库活着（v2 端点需认证属正常）。

## 二、compose 文件改造（build → image）

原 docker-compose.yml 的 postgresql / execute-migrate-all / openmetadata-server 是 `build:` 方式、ingestion 指向华为 SWR 外网镜像、ingestion 还挂载了 `/var/run/docker.sock`——**containerd 环境全都不能用**。改造点：

1. 三个 `build:` 服务 → 显式 `image:` 指向 Harbor（见镜像清单表），**否则 nerdctl compose up 会尝试本地 build 而失败**；
2. `opensearch`、`ingestion` 的 image 改为 Harbor 地址（不再依赖 Docker Hub/外网）；
3. **注释掉 ingestion 的 `/var/run/docker.sock` 挂载**——containerd 环境没有 docker.sock，不注释容器起不来（Airflow DockerOperator 功能随之不可用）；
4. 数据目录需预创建：`mkdir -p docker-volume/db-data-postgres es-data`。

## 三、部署启动（关键：分步启动）

**旧版 nerdctl compose 会忽略 `depends_on` 的 `condition`（service_healthy / service_completed_successfully），导致所有容器同时启动**——server 抢跑必然失败（见坑 2/3）。正确姿势是分步启动：

```bash
cd /data1/openmetadata-server

# 1. 先起依赖，等双 healthy
nerdctl compose -f docker-compose.containerd.yml up -d postgresql opensearch
# healthy 判断：
nerdctl exec openmetadata_opensearch curl -s http://localhost:9200/_cluster/health   # green/yellow
nerdctl exec openmetadata_postgresql pg_isready -U postgres                          # accepting connections

# 2. 跑迁移，等 Exited (0)
nerdctl compose -f docker-compose.containerd.yml up -d execute-migrate-all
nerdctl logs -f execute_migrate_all | tail -10    # 结尾 Updating services / bot users ... = 完成

# 3. 起应用
nerdctl compose -f docker-compose.containerd.yml up -d openmetadata-server ingestion
```

## 四、验证

```bash
nerdctl compose -f docker-compose.containerd.yml ps -a
curl -s http://127.0.0.1:8585/api/v1/system/health        # 1.12 起用此端点，旧 /api/v1/health-check 已 404
nerdctl exec openmetadata_server netstat -tln | grep 8585
```

外部验证（域名入口）：`https://openmetadata-row.tst-ludp.lenovo.com/api/v1/system/health` → 200 OK。

## 踩坑记录

### 坑 1：es-data 目录权限（AccessDeniedException，踩了两次）
- 症状：opensearch 启动即崩，日志 `java.nio.file.AccessDeniedException: /usr/share/opensearch/data`（或 `data/nodes`）
- 原因：compose bind mount `./es-data` 宿主目录属主是 root，而 opensearch 容器内以 **uid 1000** 运行
- 修复：`chown -R 1000:1000 /data1/openmetadata-server/es-data`，重启容器
- 二次踩坑：清库时若 `rm -rf` 删掉整个 es-data 目录再重建，新目录属主又是 root → 只清内容（`rm -rf es-data/*`）别删目录本身，删了记得重新 chown

### 坑 2：server 抢跑 → pending migrations 拒绝启动
- 症状：server 日志 `There are pending migrations to be run on the database...`
- 原因：nerdctl compose 忽略 depends_on condition，server 与迁移容器同时启动，server 检测到迁移未完成直接退出（自我保护）
- 修复：分步启动（见第三节）；迁移完成后 `nerdctl restart openmetadata_server` 即可

### 坑 3：Flowable 表半初始化残留（act_* / flw_* already exists）
- 症状：重启 server 报 `relation "act_idx_act_hi_tsk_log_task" already exists`，删完 act_* 又报 `relation "flw_ru_batch" already exists`
- 原因：第一次抢跑启动时 Flowable 工作流引擎初始化了一半表（**Flowable 7 的表前缀有 act_ 和 flw_ 两套**），二次启动建表脚本冲突
- 修复（全新环境无业务数据时最干净）：清库重来——`nerdctl compose down` → `rm -rf docker-volume/db-data-postgres/* es-data/*` → 分步启动。postgres 首次初始化会自动执行镜像内置建库脚本（openmetadata_db/airflow_db 及用户），无需手动建

### 坑 4：熵池耗尽 → opensearch JVM 启动挂起
- 症状：容器 Up 但 9200 无响应；日志停在 JVM 启动 WARNING 后不动；`top` 看 java 进程 CPU 0%；`strace -f -p <pid>` 看到某线程疯狂 `read(fd, 随机字节)` 且**每次只返回 5~6 字节**（/dev/random 熵枯竭的挤牙膏特征）；`cat /proc/sys/kernel/random/entropy_avail` 只有 44
- 原因：OpenSearch security 插件启动时大量消耗随机数，系统熵池枯竭，读 /dev/random 阻塞
- 修复：宿主机装熵服务（一劳永逸）：`yum install -y haveged && systemctl enable --now haveged`；熵池回升后卡住的 JVM 自动继续，无需重建容器。**凡是要起 Java 大应用的机器都建议常驻 haveged**
- 排查技巧：`cat /proc/<pid>/wchan`（0=用户态/空等）、`for t in /proc/<pid>/task/*/wchan; do cat $t; echo; done | sort | uniq -c`（线程等待点分布）、`strace -f -p <pid>`（跟全部线程）

### 坑 5：搜索索引缺失（UI 报 Failed to find index ...）
- 症状：UI 报 `Failed to find index openmetadata_database_search_index`、`no such index [openmetadata_service]`
- 原因：server 首次启动时 opensearch 正卡在坑 4，ES 索引初始化被跳过，reindex 缺失
- 修复：`nerdctl exec openmetadata_server bash -c 'cd /opt/openmetadata && bash bootstrap/openmetadata-ops.sh reindex --force'`
  - ⚠️ 参数是 **`--force`**，`-f` 会报 `Unknown option`
  - ⚠️ 容器内工作目录是 **`/opt/openmetadata`**（不是 /openmetadata）
  - ⚠️ 直接 exec `./bootstrap/openmetadata-ops.sh` 会 `permission denied`，用 `bash <脚本>` 显式解释执行
  - ⚠️ 长任务建议 nohup 后台 + 日志文件，防 SSH 断连中断：`nohup bash bootstrap/openmetadata-ops.sh reindex --force > /tmp/reindex.log 2>&1 &`
- 验证注意：1.12 的 reindex 先建 `*_rebuild_<时间戳>` 过渡索引，完成后**正式索引名是 alias**——`_cat/indices` 看不到 alias，要用 `_cat/aliases` 或用搜索 API 验证；页面报错消失即生效

### 坑 6：`No route to host`（端口映射看似在但连不上）
- 症状：容器 Up、`lsof` 显示有进程监听（实际是 nerdctl 的用户态端口转发代理，进程名 sleep），但 curl 报 `No route to host`
- 原因：本地防火墙（firewalld）REJECT；注意 `lsof` 看到的是**转发代理**在监听，不代表容器内应用已就绪，容器内应用没起来时代理 accept 后转发失败
- 修复：`firewall-cmd --add-port=8585/tcp ... --permanent && firewall-cmd --reload`；排查时优先在容器内自测（`nerdctl exec <容器> curl ...`）绕开宿主防火墙干扰

### 坑 7：OpenMetadata 1.12 API 端点变化
- `/api/v1/health-check`（旧）→ 1.12 起为 **`/api/v1/system/health`**，旧的返回 404
- 登录 `/api/v1/users/login` 的 `password` 字段要求 **Base-64 编码**（`admin` → `YWRtaW4=`），否则 400 `Password needs to be encoded in Base-64`；浏览器 UI 直接输明文即可

## containerd 部署的特殊性（与 docker 对比）

| 维度 | docker | containerd + nerdctl |
|---|---|---|
| 私有仓库 CA | `/etc/docker/certs.d/<host>/ca.crt` | `/etc/containerd/certs.d/<host>/ca.crt` + **hosts.toml**（server/host/ca 三段式） |
| 登录 | `docker login` | `nerdctl login`（凭据写 ~/.docker/config.json 兼容） |
| compose build | `up -d` 可本地 build | build 依赖镜像上下文，**离线/无外网时必须以 image: 显式指向仓库** |
| depends_on condition | 完整支持 | **旧版 nerdctl 忽略 condition** → 必须分步 up（依赖→迁移→应用） |
| docker.sock | 存在（DockerOperator 可用） | **不存在**，ingestion 的 docker.sock 挂载必须注释，Airflow DockerOperator 不可用 |
| 端口映射 | iptables DNAT（lsof 无监听进程） | 部分版本用户态转发代理（lsof 见 sleep 进程监听） |
| 一次性容器复用 | down+up 重跑 | 同理，升级时也要 down 后 up |
| 索引/alias 验证 | — | `_cat/indices` 不显示 alias，查 `_cat/aliases` |

## 常用命令速查

```bash
# 状态/日志
nerdctl compose -f docker-compose.containerd.yml ps -a
nerdctl logs -f openmetadata_server
nerdctl exec openmetadata_server bash -c 'cd /opt/openmetadata && bash bootstrap/openmetadata-ops.sh reindex --force'

# 健康检查
curl -s http://127.0.0.1:8585/api/v1/system/health
curl -s http://127.0.0.1:8586/healthcheck          # admin 端口

# 升级/重建（数据卷保留）
nerdctl compose -f docker-compose.containerd.yml down
nerdctl compose -f docker-compose.containerd.yml up -d postgresql opensearch
# 等 healthy → up -d execute-migrate-all → 等 Exited(0) → up -d openmetadata-server ingestion
```

## 相关笔记

- [[docker-compose-swarm-security-deep-dive]]
- [[dockerfile-best-practices]]
- [[docker-advanced-compose-swarm-security]]
