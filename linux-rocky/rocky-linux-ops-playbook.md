---
created: 2026-08-08
source: Rocky Linux 官方文档
topic: RockyLinux
subtopic: systemd/firewalld/SELinux/dnf/journalctl/cron 运维实战
priority: 🟢低
theme: linux-rocky
---

# Rocky Linux 运维实战手册 — systemd、firewalld、SELinux、dnf 官方文档细节

> 本文侧重**官方文档细节与实战排错**，与 [[rocky-linux-ops-basics]]（基础方法论篇）互补：该篇讲「是什么」，本篇讲「怎么排查、怎么加固、官方怎么说」。

## 概述

Rocky Linux 是 RHEL 的下游兼容发行版（8/9/10 系列），由社区维护。日常运维围绕五个核心领域闭环：**基础运维命令**（man/进程/网络/日志/定时任务）、**systemd 服务管理**（PID 1 并行启动，接管服务/日志/挂载/定时器）、**firewalld 防火墙**（netfilter/nftables 的动态管理前端，zone 信任模型 + 运行时/永久配置分离）、**SELinux 强制访问控制**（NSA 开发的 MAC 系统，内核在每个系统调用时查询策略）、**dnf 包管理**（Yum 的下一代替代）。全部命令在 CentOS/RHEL/Fedora 系通用。

## 架构图

![[assets/linux-rocky/diagram-rocky-ops-arch.svg]]

*图：Rocky Linux 运维五层协同架构——包管理 → 网络/安全 → systemd 服务 → 日志 → 基础工具*

## 核心概念

### systemd 与 PID 1

systemd 2010 年由 Red Hat 工程师开发，Fedora 15 首发。作为 PID 1 运行，提供服务依赖建模、开机并行启动、cgroup 进程追踪，并接管 hostname/时区/日志/挂载/socket/定时器。它是大型软件套件（最多编译 69 个二进制），默认 target 由 `/etc/systemd/system/default.target` 软链决定（只能是 multi-user 或 graphical）。

### systemctl 服务管理

语法 `systemctl [OPTIONS...] COMMAND [UNIT...]`，支持 start/stop/restart/reload/status/enable/disable/is-enabled/mask/unmask/cat/edit/show。

> ⚠️ `enable` 只设置开机自启不立即启动，需 `enable --now`；`mask` 比 disable 更强（软链到 /dev/null 彻底禁止启动）。

### Unit 文件三节结构

| 节 | 作用 | 关键指令 |
|----|------|---------|
| [Unit] | 描述与依赖 | After/Before/Requires/Wants/Conflicts |
| [Service] | 启动行为 | Type/Restart/ExecStart/KillMode/EnvironmentFile |
| [Install] | 安装行为 | WantedBy/RequiredBy/Alias |

优先级：`/usr/lib/systemd/system/`（RPM）< `/run/systemd/system/`（运行时）< `/etc/systemd/system/`（自定义，`systemctl edit` 生成 override）。

### Target 与运行级别映射

| Target | 对应 runlevel | 含义 |
|--------|--------------|------|
| graphical.target | 5 | 图形界面 |
| multi-user.target | 3 | 多用户命令行 |
| rescue.target | 1 | 单用户维护 |
| reboot.target | 6 | 重启 |
| poweroff.target | 0 | 关机 |

切换用 `systemctl isolate`，查/改默认用 `get-default` / `set-default`。

### firewalld Zone 模型

zone 描述网络连接的信任级别：drop（丢弃不回复）/block（icmp 拒绝）/public（默认公网）/external（NAT 伪装）/dmz/work/home/internal/trusted（全放行）。zone 绑定网络接口或 source IP 时才 active。

### firewall-cmd 运行时与永久配置

所有改动默认仅运行时生效，加 `--permanent` 写永久配置，或先测试再 `--runtime-to-permanent` 固化，最后 `--reload` 软重载。⚠️ `--reload` 会丢弃未固化的运行时规则。

### SELinux 安全上下文与 MAC

SELinux 是 NSA 开发的 MAC 系统，内核每次系统调用查询策略。安全上下文为 `user:role:type` 三元组（如 `system_u:object_r:httpd_sys_content_t:s0`）。查上下文用 `-Z` 系列（`id -Z` / `ls -Z` / `ps -eZ`）。

### SELinux 模式与 Boolean

三种模式：enforcing（默认拒绝+记录）/ permissive（只记录不阻断）/ disabled（不限制）。`setenforce 0|1` 临时切换，永久配置在 `/etc/sysconfig/selinux`。Boolean 是策略开关：`semanage boolean -l` 列出，`setsebool -P` 修改（⚠️ 必须带 -P 才持久化）。

### DNF 包管理器

DNF（Dandified Yum）是 Yum 的下一代替代。核心命令：install/remove/update/list/search/info/repoquery/group install/history/repolist/clean。`dnf history undo/redo ID` 可回滚整个事务。

### journalctl 日志查询

journald 是 systemd 的日志守护进程：`-u` 按 unit、`-p` 按优先级、`-b` 本次启动、`--since/--until` 时间窗、`-f` 实时、`-g` 正则、`-o json` 结构化、`--vacuum-size/time` 清理。

> ⚠️ journald 默认不持久化（Storage=auto 且无 /var/log/journal 时只存内存 /run/log/journal，重启即失）——需 `mkdir -p /var/log/journal` 或设 `Storage=persistent`。

### cron / crontab 定时任务

`crontab -e` 编辑用户任务，五字段 `m h dom mon dow` + 命令；`@daily/@weekly/@reboot` 自然时间别名。`/etc/cron.daily|weekly|monthly` dot 目录脚本默认由 **anacron** 调度（时间随机化、关机补跑）。

### nmcli / ip 网络配置

Rocky 10 起彻底移除 network-scripts/ifcfg，连接配置以 keyfile 存于 `/etc/NetworkManager/system-connections/`，用 nmcli/nmtui 管理。`ip` 命令（iproute2）仅实时查看/临时修改，重启即失。

## 常见问题表

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| firewall-cmd 添加的规则不生效或 reload 后消失 | 未加 `--permanent` 的规则仅运行时存在，reload 后丢弃 | 先运行时验证 → `--runtime-to-permanent` 固化 → `--reload` |
| 远程服务器防火墙配错 SSH 锁死 | 移除了 ssh 服务/白名单 IP 不匹配/zone target 设 drop | 绝不在远程移除 ssh；用 trusted zone + `--add-source=自己IP` 先测连通再固化；动态 IP 别用 IP 白名单（用 fail2ban） |
| 服务被 SELinux 拒绝（无报错但无效果） | enforcing 按策略拒绝，AVC 记录在 audit.log；上下文不对/boolean 未开/端口无标签 | `grep AVC /var/log/audit/audit.log \| grep denied` → `audit2why` 看建议 → semanage fcontext / setsebool / audit2allow -M |
| setsebool 修改重启后还原 | 未带 `-P` 只改运行态 | `setsebool -P httpd_can_sendmail on`；注意 `semanage boolean -l` 的 State 与 Default 两列 |
| SELinux 从 disabled 切 enforcing 后大量服务异常 | disabled 模式文件无标签，直接 enforcing 全被拒 | 设 enforcing 后 `touch /.autorelabel` + reboot 全盘重打标签 |
| semanage 命令找不到 | 属 policycoreutils-python-utils 包，默认未装 | `dnf provides */semanage` 定位 → `dnf install policycoreutils-python-utils` |
| journald 日志重启丢失 | Storage=auto 且无 /var/log/journal 时只写内存 | `mkdir -p /var/log/journal`（属主 root:systemd-journal）或设 `Storage=persistent`；`--vacuum-size=500M` 清理 |
| dnf remove 一个包连删大量依赖 | DNF 自动移除不再被需要的依赖（删 perl 连删 206 包） | 卸载前审阅 Transaction Summary；误删用 `dnf history undo <ID>` 回滚 |
| systemctl enable 后开机不启动 | [Install] 节缺 WantedBy 或改 unit 后未 daemon-reload | 检查 [Install] 节；`systemctl cat` 确认软链；改后先 `daemon-reload` 再 `enable --now` |
| crontab 到点不执行/时间漂移 | 脚本无执行权限、dot 目录走 anacron 随机化、PATH 环境不同 | `chmod +x` + 绝对路径；严格定时装 cronie-noanacron 移除 cronie-anacron；查 /var/log/cron |

## 最佳实践

1. **firewalld 先运行时测试再固化永久**：`--reload` 验证通过后再 `--runtime-to-permanent`，配错锁死时重启即可还原
2. **常用服务用服务名放行**：`--add-service=http` 而非 `--add-port=80/tcp`，自动包含正确协议端口集
3. **远程服务器永远保留 SSH 逃生通道**：public zone 默认开放 ssh 是有意设计，收紧前先建好 trusted 白名单 zone 验证可登录
4. **用 `systemctl edit` 覆盖配置**：drop-in override 优先级高于 /usr/lib/systemd/system/，包更新不覆盖
5. **用 `systemd-analyze security httpd` 评估加固**：默认新装 httpd 9.2 UNSAFE，配 CapabilityBoundingSet/NoNewPrivileges/ProtectSystem=strict 等可降到 1.5-3.0
6. **SELinux 保持 enforcing**：禁用风险自负，遇拒绝先 audit2why 翻译；临时排错用 permissive 而非 disabled
7. **利用 `dnf history undo` 事务级回滚**：出问题精确回滚某次事务，比手动反向操作安全
8. **journald 开启持久化 + logrotate 管理空间**：/var/log/journal 跨重启保留，SystemMaxUse 控制占用；rsyslog 经 imjournal 落盘文本日志
9. **网络配置统一走 nmcli**：静态 IP/DNS 用 `nmcli con mod` 持久化 + `con down/up` 激活；ip 命令仅实时查看
10. **服务器定时任务用 cronie-noanacron**：dot 目录 anacron 随机化适合笔记本，服务器装 cronie-noanacron 恢复精确调度

## 排查命令

```bash
# systemd 服务排查
systemctl status sshd                    # 服务状态与最近日志
systemctl list-dependencies multi-user.target   # 启动依赖树
systemctl cat sshd                       # 合并后的完整配置
systemd-analyze security httpd           # 服务暴露评分（0-10）

# firewalld 排查
firewall-cmd --list-all                  # 当前 zone 全部规则
firewall-cmd --get-active-zones          # 活跃 zone 与绑定接口
firewall-cmd --zone=public --add-service=http --permanent && firewall-cmd --reload

# SELinux 排查三步
sudo cat /var/log/audit/audit.log | grep AVC | grep denied | tail -1
sudo cat /var/log/audit/audit.log | grep AVC | grep denied | tail -1 | audit2why
semanage fcontext -a -t httpd_sys_content_t '/data/websites(/.*)?' && restorecon -vR /data/websites

# dnf 事务回滚
dnf history list                        # 列出事务
dnf history undo <ID>                   # 回滚指定事务

# journald 日志
journalctl -u sshd -b                   # 本次启动的 sshd 日志
journalctl --since "1 hour ago" -p err  # 最近 1 小时错误
journalctl --vacuum-size=500M           # 清理日志

# cron 排查
ls -la /etc/cron.daily/                 # 检查执行权限
grep CRON /var/log/cron | tail -20      # 实际执行记录
```

## 相关笔记

- [[rocky-linux-ops-basics]] — 基础方法论篇：Rocky Linux 分层架构、基础命令规范、systemd 单元体系（本篇的姊妹篇）
- [[docker-advanced-research-2026-07-26]] — Docker 进阶研究（Compose/Swarm/安全）

## 官方参考

- [Rocky Linux 管理指南第 16 章：systemd](https://docs.rockylinux.org/books/admin_guide/16-about-sytemd/)
- [Rocky Linux 管理指南第 17 章：日志管理](https://docs.rockylinux.org/books/admin_guide/17-log/)
- [firewalld 入门](https://docs.rockylinux.org/guides/security/firewalld-beginners/)
- [SELinux 安全指南](https://docs.rockylinux.org/guides/security/learning_selinux/)
- [systemd Unit 加固](https://docs.rockylinux.org/guides/security/systemd_hardening/)
- [DNF 包管理器](https://docs.rockylinux.org/guides/package_management/dnf_package_manager/)
- [cron 定时任务自动化](https://docs.rockylinux.org/guides/automation/cron_jobs_howto/)
- [基础网络配置（NetworkManager/nmcli/ip）](https://docs.rockylinux.org/guides/network/basic_network_configuration/)
