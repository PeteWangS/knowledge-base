# LUDP Workflow 部署文档

> 参考项目文件：
> - `ludp-module-workflow/sql/schema.sql`
> - `ludp-module-workflow/sql/data_init.sql`
> - `ludp-module-workflow/src/main/env/.env`
> - `ludp-module-workflow/src/main/resources/application.yml`
> - `ludp-module-workflow/src/main/bin/workflow.sh`

本文档按你给出的顺序整理，默认目标服务器为 Linux。

## 0. 部署前准备
![img.png](img.png)
- 已获取以下文件到服务器部署目录（示例：`/opt/ludp/workflow`）：
  - 可执行包：`workflow.jar`
  - 启动脚本：`bin/workflow.sh`
  - 依赖：`lib/` 目录
  - 环境文件：`conf/.env`
  - SQL 脚本：`sql/schema.sql`、`sql/data_init.sql`
  - 前端资源：`dist.tar.gz`
- PostgreSQL 可连接，且具备建库与建表权限。

示例目录：

```bash
/opt/ludp/workflow/
  workflow.jar
  bin/workflow.sh
  conf/.env
  sql/schema.sql
  sql/data_init.sql
```

---

## 1. 创建数据库 `workflow`

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_ADMIN_USER> -d postgres -c "CREATE DATABASE workflow;"
```

可选校验：

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_ADMIN_USER> -d postgres -c "\l"
```

---

## 2. 导入 `schema.sql` 脚本

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -f /opt/ludp/workflow/schema.sql
```

可选校验（查看核心表是否创建）：

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -c "\dt"
```

---

## 3. 导入 `data_init.sql`

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -f /opt/ludp/workflow/data_init.sql
```

可选校验（检查初始化数据）：

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -c "select count(*) from email_account;"
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -c "select count(*) from wf_global_default_conf;"
```

---

## 4. 修改 `email_account` 初始化 SMTP 配置

`data_init.sql` 默认插入了 1 条 SMTP 配置，请按实际邮箱网关更新（联想走smtp需要提前申请白名单）：

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -c "
update email_account
set host = '<SMTP_HOST>',
    port = <SMTP_PORT>,
    username = '<SMTP_USERNAME>',
    password = '<SMTP_PASSWORD>',
    properties = '{\"mail.smtp.auth\": \"false\", \"mail.smtp.user\": \"<SMTP_USERNAME>\", \"mail.smtp.timeout\": \"30000\", \"mail.smtp.sendpartial\": \"false\", \"mail.smtp.writetimeout\": \"30000\", \"mail.transport.protocol\": \"smtp\", \"mail.smtp.connectionpool\": \"true\", \"mail.smtp.connectiontimeout\": \"5000\", \"mail.smtp.connectionpoolsize\": \"60\"}',
    updater = 'system',
    update_time = now()
where id = 1;
"
```

校验：

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -c "select id,host,port,username,update_time from email_account;"
```

---

## 5. 修改 `wf_global_default_conf` 的部分配置

建议先查询，再按实际环境更新（示例包含常改项）：

```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -c "
select key, value, system_name, environment_name,comment
from wf_global_default_conf
where key in ('sender_express','completed_ccRecipients_express','admin_itcodes','ai support')
order by key;
"
```
修改示例：
```bash
psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d workflow -c "
update wf_global_default_conf
set value = 'QLExpress(''''noreply@your-domain.com'''')', updater = 'system', update_time = now()
where key = 'sender_express' and system_name = 'LUDP' and environment_name = 'PRC';

update wf_global_default_conf
set value = 'QLExpress(''''ops@your-domain.com'''')', updater = 'system', update_time = now()
where key = 'completed_ccRecipients_express' and system_name = 'LUDP' and environment_name = 'PRC';

update wf_global_default_conf
set value = 'itcode1,itcode2', updater = 'system', update_time = now()
where key = 'admin_itcodes' and system_name = 'LUDP' and environment_name = 'PRC';

update wf_global_default_conf
set value = 'false', updater = 'system', update_time = now()
where key = 'ai support' and system_name = 'LUDP' and environment_name = 'PRC';
"
```

---

## 6. 修改 `.env` 文件配置

文件参考：`开发：ludp-module-workflow/src/main/env/.env；部署：conf/.env`。

> 重要：.env文件需要提前配置，缺失配置无法正常启动，且启动脚本会读取该文件的配置。

建议至少配置以下变量：

```dotenv
SERVER_PORT=18080
WORKFLOW_PG_IP=<PG_HOST>
WORKFLOW_PG_PORT=5432
WORKFLOW_PG_DATABASE=workflow
WORKFLOW_PG_SCHEMA=public
WORKFLOW_PG_USERNAME=<PG_USER>
WORKFLOW_PG_PASSWORD=<PG_PASSWORD>

LEAP_PRC_MYSQL_IP=<LEAP_PRC_MYSQL_IP>
LEAP_PRC_MYSQL_PORT=3306
LEAP_PRC_MYSQL_DATABASE=<LEAP_PRC_MYSQL_DATABASE>
LEAP_PRC_MYSQL_USERNAME=<LEAP_PRC_MYSQL_USERNAME>
LEAP_PRC_MYSQL_PASSWORD=<LEAP_PRC_MYSQL_PASSWORD>

LEAP_ROW_MYSQL_IP=<LEAP_ROW_MYSQL_IP>
LEAP_ROW_MYSQL_PORT=3306
LEAP_ROW_MYSQL_DATABASE=<LEAP_ROW_MYSQL_DATABASE>
LEAP_ROW_MYSQL_PASSWORD=<LEAP_ROW_MYSQL_PASSWORD>

PORTAL_DOMAIN=<PORTAL_DOMAIN>
WORKFLOW_DOMAIN=<WORKFLOW_DOMAIN>
USERADMIN_DOMAIN=<USERADMIN_DOMAIN>

FILE_BASE_PATH=/data/ludp/workflow/
AI_BASE_URL=大模型地址
AI_API_KEY=模型apikey
AI_MODEL=模型名称
```

> 注意：`FILE_BASE_PATH` 需要替换为实际配置，且以 `/` 结尾，避免拼接路径异常。

---

## 8. 下载并安装 OpenJDK 21

方式一（包管理器，按发行版选择）：

```bash
#ubantu/Debian
sudo apt-get update
sudo apt-get install -y openjdk-21-jdk
java -version
```

```bash
#centos/RHEL
sudo yum install -y java-21-openjdk java-21-openjdk-devel
java -version
```

方式二（手工安装到固定目录）：

```bash
mkdir -p /opt/java
cd /opt/java
# 将 OpenJDK 21 压缩包上传到该目录后解压
tar -xzf OpenJDK21U-jdk_x64_linux_hotspot_*.tar.gz
ls -l
```

---

## 9. 根据启动脚本直接启动

进入部署目录后执行：

```bash
cd /opt/ludp/workflow
bash workflow.sh start /opt/java/<jdk-21-dir>
```
> 注意：如果配置了JAVA_HOME且JDK版本为21+，启动参数可以不带JDK目录。

查看进程与日志：

```bash
ps -ef | grep workflow.jar
ls -l workflow.pid
tail -n 200 /opt/ludp/workflow/logs/app.log
```

停止服务：

```bash
bash workflow.sh stop
```

---

### 10. Nginx 反向代理配置示例

```nginx
server {
  listen    80;
  server_name <WORKFLOW_DOMAIN>;
  listen 443 ssl;
  ssl_certificate xxx.cer;
  ssl_certificate_key xxx.key;
  client_body_buffer_size 128k;
  client_max_body_size 600m;
  large_client_header_buffers 4 32k;
  client_header_buffer_size  1k;
  ssl_session_timeout 10m;
  ssl_protocols TLSv1 TLSv1.1 TLSv1.2;
  ssl_prefer_server_ciphers on;
  access_log  /home/access_ludpworkflow.log  main;
  charset utf-8;
  location /workflow/api/ {
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass http://<WORKFLOW_SERVER_IP>:<WORKFLOW_SERVER_PORT>/api/;
  }

  location /workflow/ {
    alias <WORKFLOW_FROENTEND>/dist/;
    try_files $uri $uri/ /workflow/index.html;
  }

  location /wfmedia {
    alias <FILE_BASE_PATH>;
  }
}

```
> 注意：之前Workflow和Potal公用一个域名，如果是基于之前的Ngnix配置修改，在原来的配置上把WORKFLOW_SERVER_IP，WORKFLOW_SERVER_PORT，WORKFLOW_FROENTEND，FILE_BASE_PATH替换为实际的配置即可
> WORKFLOW_SERVER_IP：部署服务器IP。
> WORKFLOW_SERVER_PORT：部署服务器端口，与.env文件的SERVER_PORT变量一致。
> WORKFLOW_FROENTEND：前端dist目录路径（提前解压和放好dist.tar.gz前端资源包）。
> FILE_BASE_PATH：文件存储路径，与.env文件的FILE_BASE_PATH变量一致。
---

## 附：快速排查

1. 启动后秒退：优先检查 `.env` 是否与 `workflow.sh` 同目录，以及 `WORKFLOW_PG_*` 是否正确。
2. 数据库连接失败：检查 PostgreSQL 用户权限、网络连通性、`workflow` 库是否已建并导入脚本。
3. 启动脚本找不到 Java：确认命令第二个参数是 JDK 根目录（包含 `bin/java`）。

