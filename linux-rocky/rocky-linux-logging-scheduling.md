---
created: 2026-08-22
source: Rocky Linux 官方文档
topic: RockyLinux
subtopic: 日志管理与自动化运维：rsyslog/logrotate/journald/journalctl 与 cron/anacron 定时任务
priority: 🟢低
theme: linux-rocky
---

# Rocky Linux 日志管理与自动化运维 — rsyslog、journald、logrotate 与 cron/anacron 定时任务

> 本文基于 Rocky Linux 官方管理指南第 17 章（日志）与 automation 指南三篇（cron_jobs_howto / cronie / anacron）整理，与 [[rocky-linux-ops-basics]]（基础方法论）、[[rocky-linux-ops-playbook]]（运维实战）、[[rocky-linux-security-hardening]]（安全加固）互补，本篇聚焦**日志定位、轮转配置与定时任务排错**三大日常场景。

## 概述

Rocky Linux 8/9 的运维日志体系是「双轨制」：rsyslog 负责把日志按规则写入 `/var/log/` 下的文本文件（messages、secure、cron、maillog、boot.log 等），journald 作为 systemd 组件以二进制结构化方式记录 boot/kernel/application 全部日志，二者通过 imjournal 输入模块集成、默认共存互不影响。日志无限增长由 logrotate 解决（轮转、压缩、删除三职能）。自动化调度体系同样双轨：cron（cronie/crond）按严格时刻执行任务，适合 24x7 服务器；anacron 以天为单位补跑任务，适合不常开机的笔记本与工作站；systemd timer 提供原生定时器替代方案。

## 架构图

![[assets/linux-rocky/diagram-log-dual-track.svg]]
*图：日志双轨制架构 — 应用/内核/启动日志进入 journald 二进制域，经 imjournal 集成进 rsyslog 文本域，最终由 logrotate 轮转归档*

**日志链路**：应用与内核产生日志 → journald（systemd-journald 守护进程）默认写入 `/run/log/journal/` 下的二进制 system.journal（易失，重启清空；配置 `Storage=persistent` 后落盘 `/var/log/journal/`）→ rsyslog 通过 imjournal 模块（`StateFile=imjournal.state` 记录读取位置）从 journald 集成日志，按 `/etc/rsyslog.conf` 及 `/etc/rsyslog.d/` 中的规则（`selector=facility.priority` + action）分流写入 `/var/log/messages`、`/var/log/secure`、`/var/log/cron` 等文本文件 → logrotate（`/etc/logrotate.conf` 全局 + `/etc/logrotate.d/` 分应用规则，状态记录在 `/var/lib/logrotate/logrotate.status`）按周期或体积轮转归档压缩旧日志。

## 核心概念

| 概念 | 说明 | 关键点 |
|------|------|--------|
| rsyslog | 传统 syslog 的升级版，快速收集并处理日志的程序，负责把日志按规则写入 /var/log/ 下各文本文件。主配置 /etc/rsyslog.conf + 扩展目录 /etc/rsyslog.d/，支持三种配置格式：basic（单行 facility.priority action）、advanced（RainerScript，灵活精确）、legacy（已弃用） | 规则行由 selector（facility.priority，如 cron.*、authpriv.*）与 action（如 /var/log/cron、:omusrmsg:* 发给所有登录用户）两部分组成 |
| journald | systemd 内置日志守护进程，接管 boot 日志、内核日志与应用日志，按 priority（数值 0=emerg 至 7=debug）与 facility 标记，以二进制形式存于 system.journal | 默认不持久化——日志暂存 /run/log/journal/，重启即清空；启用持久化需 Storage=persistent 且 /var/log/journal 可写 |
| journalctl | 解析 journald 二进制日志的唯一命令，journalctl [OPTIONS...] [MATCHES...] | -u 按 unit 过滤（可多次）、-b 当前启动、-p 优先级范围、--since/--until 时间窗、-f 跟随、-x 附加解释、-e 跳到末尾、-r 倒序 |
| logrotate | 日志轮转工具，解决日志持续增长导致的存储占用与性能下降：Rotation（按时间/体积归档旧日志并新建空文件）、Compress（压缩归档日志）、Delete（按策略删除过期日志） | 默认 /etc/logrotate.conf 为 weekly + rotate 4 + create + dateext + include /etc/logrotate.d；手工调试用 logrotate -v / -f |
| cron / cronie | 基于时间的任务调度系统。cronie 是 Rocky Linux 默认实现（包名 cronie，守护进程 crond.service），crontab 命令维护各用户任务表，任务记录在 /var/spool/cron/<user>，执行日志在 /var/log/cron | 最小时间粒度 1 分钟；脚本必须 chmod +x 且输出重定向（如 &> /dev/null），否则输出会阻塞任务 |
| anacron | 以天为单位的补跑调度器（cronie 的一部分，包 cronie-anacron），适合不 24x7 运行的笔记本/工作站：机器在计划时刻关机则任务错过，下次开机后补跑。与 crontab 互补而非替代 | /etc/anacrontab 格式：period delay job-id command（如 1 5 cron.daily nice run-parts /etc/cron.daily）；RANDOM_DELAY=45 随机延迟、START_HOURS_RANGE=3-22 工作窗口 |
| crontab 五字段 | crontab 条目为五字段 + 命令：分（0-59）、时（0-23）、日（1-31）、月（1-12）、周（0-7，0 与 7 均表示周日）。支持特殊符号 *（任意）、,（列举，如 8,12,16）、-（范围，如 1-5）、*/n（间隔，如 */10 每 10 分钟） | @options 直接替代五字段：@hourly/@daily/@weekly/@monthly/@yearly/@reboot，且绕过 anacron 直接由 crond.service 执行 |
| systemd timer | systemd 原生定时器单元（unit 类型 timer），功能类似 cron，用于按计划激活其他单元，详见 man 5 systemd.timer。系统内置 timers.target 下挂载 dnf-makecache.timer、systemd-tmpfiles-clean.timer 等 | timer 是 systemd 十二种 unit 类型之一，与 service/socket/path 等并列，通过 timers.target 同步启停 |

![[assets/linux-rocky/diagram-scheduler-dual-track.svg]]
*图：调度体系双轨架构 — crond 严格时刻执行用户/系统任务表与 dot 目录；anacron 以随机延迟在工作窗口内补跑 daily/weekly/monthly；systemd timer 原生定时器独立成轨*

**调度链路**：crond.service（cronie 守护进程）读取用户 crontab（`/var/spool/cron/<user>`）与 `/etc/crontab`、`/etc/cron.d/` 按五字段严格时刻执行；dot 目录 `/etc/cron.hourly` 由 `/etc/cron.d/0hourly` 每小时第 1 分钟经 run-parts 调用（不受 anacron 影响），`/etc/cron.daily|weekly|monthly` 默认由 anacron（`/etc/anacrontab`，随机延迟 0-45 分钟、工作窗口 3-22 点）补跑；systemd timers.target 挂载 dnf-makecache.timer 等原生定时器。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方参考 |
|------|------|----------|----------|
| 重启后 journald 日志全部丢失，历史日志查不到 | journald 默认不启用持久化：Storage=auto 时仅当 /var/log/journal 目录存在才落盘，全新系统未创建该目录则日志只暂存在 /run/log/journal/（易失目录），重启即清空 | 启用持久化：创建 /var/log/journal 目录（或直接在 /etc/systemd/journald.conf 设 Storage=persistent）并重启 systemd-journald；此后日志写入 /var/log/journal/，可用 journalctl --list-boots 跨启动查询 | 管理指南 17-log |
| cron 任务到点不执行，/var/log/cron 无对应记录 | 常见两类：① 脚本没有执行权限（cron 只运行可执行文件）；② 命令输出未重定向——cron 会把输出邮件给任务属主，无邮件服务时任务执行受阻 | 脚本 chmod +x；命令追加输出重定向如 `*/10 * * * * /usr/local/sbin/backup &> /dev/null`；用 journalctl -u crond.service 与 /var/log/cron* 排查守护进程与任务执行记录 | automation/cronie |
| 服务器上 /etc/cron.daily 等 dot 目录任务执行时间不严格、每天漂移 | 默认安装了 cronie-anacron，daily/weekly/monthly 目录由 anacron 调度，带 0-45 分钟随机延迟且只在 3-22 点窗口内补跑，适合工作站但不符合服务器严格时刻需求 | 服务器改为 cron 严格调度：dnf install cronie-noanacron 并 dnf remove cronie-anacron，之后由 /etc/cron.d/dailyjobs 控制：`02 4 * * * run-parts /etc/cron.daily`、`22 4 * * 0` 跑 weekly、`42 4 1 * *` 跑 monthly | automation/cron_jobs_howto |
| /var/log 目录持续膨胀，磁盘被日志占满 | 日志无限增长：文本日志缺少轮转策略（如编译安装的应用没有自带 logrotate 规则），或 journald 容量无上限（SystemMaxUse/SystemMaxFileSize 未配置） | 为应用配置 /etc/logrotate.d/ 规则（rotate 保留份数、compress 压缩、minsize 体积条件、dateext 日期后缀）；journald 侧在 journald.conf 设 SystemMaxUse/SystemMaxFileSize/MaxRetentionSec，并用 journalctl --vacuum-size/--vacuum-time 立即清理；journalctl --disk-usage 查看占用 | 管理指南 17-log |
| cat /var/log/btmp 或 /var/log/wtmp 显示乱码 | btmp、wtmp、lastlog 是二进制格式日志文件（分别记录登录失败、登录登出/启停、最后登录时间），不能直接 cat 查看 | 使用配套命令解析：登录失败用 lastb（/var/log/btmp）、登录记录用 last（/var/log/wtmp）、最后登录用 lastlog（/var/log/lastlog）；纯文本日志（messages/secure/cron/boot.log）才可直接查看 | 管理指南 17-log |
| crontab -e 与直接编辑 /etc/crontab 行为不一致 | 两者格式与作用域不同：crontab -e 维护当前登录用户的任务表（/var/spool/cron/<user>），条目无需指定用户；/etc/crontab 是系统级文件，每行第 6 列必须显式指定 user-name，格式为 `* * * * * user-name command` | 个人定时任务用 crontab -e（自动归属当前用户）；系统级任务用 /etc/crontab 并务必写全用户列；查看他人任务用 crontab -l -u <username> | automation/cronie |
| rsyslog 与 journald 双轨并存造成额外性能开销 | 默认配置下 rsyslog 通过 imjournal 输入模块从 journald 集成日志（StateFile=imjournal.state 记录读取位置），两条链路同时运行，对吞吐与内存敏感的场景不划算 | 性能优先且不需要结构化日志时可切纯 socket 模式：rsyslog 配置 imuxsock 且 SysSock.Use=on（关闭 imjournal），journald 设 Storage=none + ForwardToSyslog=yes 把所有日志转发给 rsyslog 写文本，重启生效；代价是失去结构化日志能力 | 管理指南 17-log |

## 最佳实践

1. **故障排查先看 messages 与 secure**：系统报错先查 /var/log/messages（系统级核心信息），涉及登录、su 切换、用户/密码变更等身份认证问题查 /var/log/secure（authpriv 独立存放），二者均为纯文本可直接查看。
2. **二进制日志必须用专用命令解析**：btmp 用 lastb、wtmp 用 last、lastlog 用 lastlog 命令查看，不要 cat 二进制文件；journald 的 system.journal 必须用 journalctl 解析。
3. **仓库安装的软件无需改轮转规则，编译安装必须自配**：从仓库安装的软件包自带 /etc/logrotate.d/ 轮转规则（如 syslog、chrony、firewalld），一般无需修改；源码编译安装的应用没有规则，必须手工编写轮转配置，否则日志无限增长。
4. **cron 脚本三要素：可执行、输出重定向、日志留痕**：脚本需 chmod +x；命令输出重定向（&> /dev/null 或追加到日志文件）避免阻塞；排错时查 /var/log/cron 与 journalctl -u crond.service。记住 cron 最小时间粒度是 1 分钟。
5. **按机器类型选择调度器：服务器用 cron，笔记本/工作站用 anacron**：服务器 24x7 运行且任务需精确时刻（如备份），用 crontab/@options 严格调度，必要时卸载 anacron 换 cronie-noanacron 让 dot 目录也严格化；不常开机的机器用 anacron 保证错过任务开机补跑。两者可混用。
6. **敏感日志隔离与 journald 防篡改**：rsyslog 默认规则已把 authpriv.* 独立写入 /var/log/secure；journald 持久化时保持 Seal=yes（FSS 前向安全密封）防止日志条目被恶意篡改，并合理设置 RateLimitIntervalSec/RateLimitBurst 与 SystemMaxUse 限制资源占用。
7. **anacrontab 修改后用 -T 校验**：编辑 /etc/anacrontab 后先用 anacron -T 测试配置文件有效性；-f 强制运行所有任务（忽略时间戳）、-u 只更新时间戳不执行；观察执行流程可用 journalctl -u crond.service。

## 排查命令

![[assets/linux-rocky/diagram-troubleshoot-flow.svg]]
*图：日志与定时任务排查流程 — 先按日志类型定位，再逐项排查 journald 持久化、cron 执行失败与磁盘膨胀三类高频问题*

| 场景 | 命令 |
|------|------|
| 按服务过滤 journald 日志 | journalctl -u sshd.service（-u 可多次） |
| 本次启动日志 | journalctl -b |
| 优先级过滤（err 及以上） | journalctl -p 3（等价于 -p 0..3） |
| 时间窗过滤 | journalctl --since "2026-08-22 00:00" --until "2026-08-22 08:00" |
| 实时跟随 / 跳到末尾 / 倒序 | journalctl -f / journalctl -e / journalctl -r |
| journald 磁盘占用 / 立即轮转 | journalctl --disk-usage / journalctl --rotate |
| journald 空间清理 | journalctl --vacuum-size=500M / --vacuum-time=30d |
| 跨启动查询 / 启用持久化 | journalctl --list-boots；mkdir /var/log/journal && systemctl restart systemd-journald（或 journald.conf 设 Storage=persistent） |
| logrotate 调试 / 强制轮转 | logrotate -d /etc/logrotate.conf（dry-run 调试）/ logrotate -v -f /etc/logrotate.conf（强制） |
| cron 执行记录 | 查看 /var/log/cron 与 journalctl -u crond.service |
| 用户任务表查看 / 编辑 | crontab -l / crontab -e；查看他人 crontab -l -u <username> |
| anacron 校验 / 强制 / 时间戳 | anacron -T / anacron -f / anacron -u |
| 二进制登录日志解析 | lastb（btmp）、last（wtmp）、lastlog（lastlog） |

## 相关笔记

- [[rocky-linux-ops-basics]] — Rocky Linux 基础方法论（含日志与定时任务入门）
- [[rocky-linux-ops-playbook]] — systemd/firewalld/SELinux/dnf 运维实战（journalctl 视角）
- [[rocky-linux-security-hardening]] — 安全加固（/var/log/secure 审计与 SELinux 日志分析）

## 参考资料

- [Rocky Linux 管理指南第 17 章：日志管理（rsyslog/logrotate/journald/journalctl）](https://docs.rockylinux.org/books/admin_guide/17-log/)
- [Rocky Linux 管理指南第 16 章：systemd（unit 类型/timers/系统管理命令）](https://docs.rockylinux.org/books/admin_guide/16-about-sytemd/)
- [Rocky Linux 指南：使用 cron 与 crontab 自动化进程](https://docs.rockylinux.org/guides/automation/cron_jobs_howto/)
- [Rocky Linux 指南：cronie 定时任务](https://docs.rockylinux.org/guides/automation/cronie/)
- [Rocky Linux 指南：anacron 自动化命令](https://docs.rockylinux.org/guides/automation/anacron/)
- [crontab(5) 手册页（五字段与 @options 权威定义）](https://man7.org/linux/man-pages/man5/crontab.5.html)
- [journald.conf(5) 手册页（Storage/RateLimit/SystemMaxUse 等完整键）](https://man7.org/linux/man-pages/man5/journald.conf.5.html)
