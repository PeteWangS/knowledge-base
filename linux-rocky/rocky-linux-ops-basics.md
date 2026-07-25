---
created: 2026-07-25
source: GitHub Official Repos / Community
topic: RockyLinux
priority: 🟢低
theme: linux-rocky
---

# Rocky Linux — 基础运维命令、systemd 服务管理、firewalld 防火墙、SELinux、dnf 包管理

## 概述

Rocky Linux 是一个社区驱动的企业级操作系统，设计目标是与 Red Hat Enterprise Linux (RHEL) 实现 100% bug-for-bug 兼容。它由 CentOS 创始人 Gregory Kurtzer 在 CentOS 转向滚动发布模型后创建，旨在提供稳定的开源企业计算基础。使用 RPM 包格式和 DNF 包管理器，systemd 作为初始化和服务管理器，firewalld 作为默认防火墙管理器，SELinux 实现强制访问控制。

**当前支持版本**：Rocky Linux 8、9、10

## 架构图

![[assets/linux-rocky/diagram-rocky-linux-architecture.svg]]

*图：Rocky Linux 分层架构——从 Linux Kernel 到应用服务的完整技术栈*

## 核心概念

### 基础运维命令规范

Rocky Linux 基础命令遵循 `command [options] [arguments]` 模式：
- 短选项以单横线开头并可组合：`ls -lia`
- 长选项以双横线开头：`ls --all`
- man 手册分为 8 个章节，通过 `man [section] command` 查阅
- `apropos` 按关键字搜索，`whatis` 显示一行描述

常用命令分类：
| 分类 | 命令 |
|------|------|
| 文件操作 | cd, ls, pwd, cp, mv, rm, touch, cat, less |
| 文本处理 | grep, sed, awk, cut, sort, uniq |
| 进程管理 | ps, top, kill, pgrep |
| 权限管理 | chmod, chown, chgrp |
| 用户管理 | useradd, usermod, passwd, id |
| 磁盘管理 | df, du, fdisk, mount |
| 系统信息 | uname, hostnamectl, lscpu, free |

### systemd 服务管理器

![[assets/linux-rocky/diagram-systemd-flow.svg]]

*图：systemd 单元类型体系及 SysV 运行级别映射关系*

systemd 作为 PID 1 运行，提供：
- 并行的服务启动加速系统启动
- 按需守护进程激活（socket/D-Bus 激活）
- cgroups 进程跟踪和管理
- 事务性依赖关系服务控制

**单元类型**：service（服务）、socket（IPC 套接字）、timer（定时任务，替代 cron）、target（运行级别）、mount（挂载点）、path（路径激活）、slice（cgroup 资源控制）

**关键命令**：
```bash
# 服务生命周期管理
systemctl start|stop|restart|reload|enable|disable|status|mask|cat|edit [unit]

# 系统状态
systemctl list-units          # 列出活动单元
systemctl list-unit-files     # 列出所有单元文件
systemctl get-default         # 查看默认目标
systemctl set-default [target] # 设置默认目标

# 性能分析
systemd-analyze blame         # 显示服务启动耗时
systemd-analyze verify [unit] # 验证单元文件语法

# 日志查看
journalctl -u [service] -n 50 --no-pager   # 服务日志
journalctl -f                               # 实时跟踪
journalctl --since yesterday                # 按时间过滤
journalctl -p err                           # 仅错误级别
```

**配置目录**：
- `/usr/lib/systemd/system/` — 包安装的单元文件
- `/run/systemd/system/` — 运行时单元文件
- `/etc/systemd/system/` — 管理员覆盖（优先级最高）

**目标映射**：`runlevel0→poweroff.target`、`1→rescue.target`、`3→multi-user.target`、`5→graphical.target`、`6→reboot.target`

### DNF 包管理器

DNF (Dandified YUM) 是 RPM 包管理器的下一代前端：

```bash
sudo dnf install [pkg]       # 安装包
sudo dnf remove [pkg]        # 卸载（含依赖检查）
sudo dnf update/upgrade      # 系统更新
sudo dnf list                # 列出包
sudo dnf search [keyword]    # 搜索包
sudo dnf info [pkg]          # 包信息
sudo dnf group install "Group"  # 组安装
sudo dnf history list        # 事务历史
sudo dnf history undo [id]   # 回滚事务
```

**仓库**：baseos（基础 OS）、appstream（应用流）、crb（CodeReady Builder）、epel（Extra Packages）

**技巧**：`dnf provides */command` 查找哪个包提供指定命令；`sudo dnf history undo [id]` 精确回滚事务

### SELinux 强制访问控制

SELinux 在内核层面实现 Mandatory Access Control (MAC)，独立于传统的 Unix DAC 权限。

**三种模式**：
| 模式 | 行为 |
|------|------|
| enforcing（默认） | 强制执行策略，拒绝未授权访问 |
| permissive | 仅记录日志，不阻止操作 |
| disabled | 完全禁用 |

**上下文格式**：`user_u:role_r:type_t`（进程称为 domain，文件称为 type）

**常用命令**：
```bash
getenforce                    # 查看当前模式
setenforce 0|1               # 运行时切换（无需重启）
sestatus                      # 全面状态信息
semanage boolean -l           # 列出布尔开关
setsebool -P [boolean] on     # 启用布尔开关
audit2why                     # 解释 AVC 拒绝原因
audit2allow -M [module]       # 创建自定义策略模块
```

**SELinux 拒绝日志**：位于 `/var/log/audit/audit.log`，包含 `AVC denied` 条目。使用 `audit2why` 诊断，`audit2allow -M` 创建自定义策略模块。使用 `-Z` 参数（如 `ls -Z`、`ps -Z`）查看 SELinux 上下文。

### firewalld 动态防火墙

![[assets/linux-rocky/diagram-firewalld-zones.svg]]

*图：firewalld 区域安全模型——从 drop（完全丢弃）到 trusted（完全信任）的八个安全等级*

firewalld 是 nftables/netfilter 的前端，采用**区域（zone）安全模型**：

| 区域 | 信任等级 | 典型用途 |
|------|---------|---------|
| drop | 🔴最低 | 静默丢弃所有入站流量 |
| block | 🟠高 | 拒绝（icmp 不可达）所有入站 |
| public | 🟡默认 | 不受信公共网络 |
| external | 🟡中 | NAT/伪装的外部网络 |
| dmz | 🟢中 | DMZ 区域，有限服务 |
| work | 🔵中高 | 工作网络 |
| internal | 🔵高 | 内部网络 |
| trusted | 🟢最高 | 接受所有连接 |

**关键命令**：
```bash
firewall-cmd --get-zones                    # 列出所有区域
firewall-cmd --get-active-zones             # 当前活跃区域
firewall-cmd --set-default-zone=public      # 设置默认区域
firewall-cmd --zone=public --add-service=http   # 允许 HTTP
firewall-cmd --zone=public --add-port=8080/tcp  # 允许端口
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.0/24" service name="ssh" accept'
firewall-cmd --runtime-to-permanent         # 运行时规则持久化
firewall-cmd --reload                       # 重载永久配置
```

## 常见问题与解决方案

| 问题 | 原因 | 方案 | 官方链接 |
|------|------|------|---------|
| SELinux 阻止应用访问（如 Web 服务器读不到文件） | 文件缺少正确的 SELinux 上下文类型 | `semanage fcontext -a -t httpd_sys_content_t "/data/websites(/.*)?"` 然后 `restorecon -vR` | [Rocky Linux SELinux Guide](https://docs.rockylinux.org/guides/security/learning_selinux/) |
| firewalld 配置重启后丢失 | 默认运行时规则不持久 | `firewall-cmd --runtime-to-permanent` 或添加 `--permanent` 标志 | [firewalld Beginners Guide](https://docs.rockylinux.org/guides/security/firewalld-beginners/) |
| DNF 更新后系统损坏 | 包安装引入了不兼容依赖 | `sudo dnf history list` → `sudo dnf history undo [id]` | [DNF Package Manager Guide](https://docs.rockylinux.org/guides/package_management/dnf_package_manager/) |
| SSH 连接被防火墙规则拒绝 | 从活动区域移除了 SSH 服务或 IP 受限 | 立即 `firewall-cmd --zone=public --add-service=ssh` | [firewalld Beginners Guide](https://docs.rockylinux.org/guides/security/firewalld-beginners/) |
| SELinux 在 permissive 模式下需诊断被阻止的操作 | 需分析 AVC 拒绝日志 | `audit2why` 分析 → `audit2allow -M mymodule` 创建策略 | [SELinux Guide](https://docs.rockylinux.org/guides/security/learning_selinux/) |
| systemd 服务启动失败 "Unit not found" | 依赖缺失/单元文件语法错误 | `systemd-analyze verify` 检查语法，`systemctl list-dependencies` 检查依赖 | [systemd Guide](https://docs.rockylinux.org/books/admin_guide/16-about-sytemd/) |
| DNF 更新报 checksum/GPG key 错误 | 元数据缓存损坏或 GPG 密钥缺失 | `sudo dnf clean all && sudo dnf makecache` | [DNF Package Manager Guide](https://docs.rockylinux.org/guides/package_management/dnf_package_manager/) |

## 官方最佳实践

### 1. systemd：使用 drop-in 覆盖而非直接修改单元文件

`systemctl edit [unit]` 在 `/etc/systemd/system/[unit].d/override.conf` 创建覆盖配置，确保包更新不会覆盖自定义设置。

```bash
# 最佳实践：使用 drop-in 覆盖
sudo systemctl edit myservice
# 然后添加修改的 [Service] 部分即可
```

### 2. firewalld：先测试规则再持久化

添加规则时不加 `--permanent` 先测试，确认无误后再 `firewall-cmd --runtime-to-permanent`。如果规则锁住了远程连接，`systemctl restart firewalld` 即可恢复。

### 3. SELinux：优先使用布尔开关而非创建自定义策略

```bash
# 先检查是否有现成布尔开关
semanage boolean -l | grep [service_name]

# 启用布尔开关（推荐方案）
sudo setsebool -P [boolean] on

# 仅作为最后手段：创建自定义策略模块
sudo audit2allow -M mylocalmodule
sudo semodule -i mylocalmodule.pp
```

### 4. DNF：使用组安装简化软件集合部署

```bash
# 组安装示例
sudo dnf group install "Development Tools"
sudo dnf group install "KDE Plasma Workspaces"
sudo dnf install @container-management
```

### 5. SELinux：禁止在生产环境禁用

出现问题应切到 permissive 模式诊断然后回到 enforcing，而不是完全禁用。禁用需要重启并 relabel 整个文件系统。

### 6. firewalld：创建自定义区域实现精细访问控制

使用 `--add-source=IP` 基于客户端来源激活区域，配合 rich rules 实现 IP 级粒度。

### 7. DNF：启用自动安全更新

```bash
sudo dnf install dnf-automatic
# 编辑 /etc/dnf/automatic.conf 配置
sudo systemctl enable --now dnf-automatic.timer
```

### 8. systemd：使用 journalctl 进行日志分析

```bash
journalctl -u [service]       # 服务日志
journalctl --since "1 hour ago"  # 时间过滤
journalctl -p err -b          # 本次启动的错误日志
journalctl -k                 # 内核日志
```

## 排查命令速查

```bash
# 系统信息
uname -a                     # 内核版本
hostnamectl                  # 系统信息
lscpu                        # CPU 信息
free -h                      # 内存使用
df -h                        # 磁盘使用

# SELinux
getenforce                   # 当前模式
sudo cat /var/log/audit/audit.log | grep AVC | grep denied | tail -5
audit2why < /var/log/audit/audit.log

# firewalld
firewall-cmd --list-all      # 当前规则
firewall-cmd --get-active-zones
firewall-cmd --zone=public --list-services

# systemd
systemctl list-units --type=service --state=running
systemd-analyze blame | head -10
journalctl -p 3 -xb          # 本次启动错误日志

# DNF
sudo dnf repolist            # 仓库列表
sudo dnf history list        # 事务历史
sudo dnf check-update        # 检查可用更新
```

## 相关笔记
- [[systemd-timers-cron-alternative]]
- [[selinux-basics-policy-management]]
- [[dnf-package-management-tips]]
