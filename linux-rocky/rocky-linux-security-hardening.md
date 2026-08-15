---
title: Rocky Linux 安全加固官方指南深度笔记（systemd 加固 / firewalld / SELinux / DNF）
created: 2026-08-15
tags: [rocky-linux, systemd, firewalld, selinux, dnf, security, hardening]
---

# Rocky Linux 安全加固官方指南深度笔记

## 概述

本轮 Rocky Linux 研究聚焦官方文档（rocky-linux/documentation 仓库）的深度细节，与既有两篇笔记互补：

- [[rocky-linux-ops-basics]]（基础方法论篇）讲「是什么」
- [[rocky-linux-ops-playbook]]（实战排错篇）讲「怎么排查」
- **本篇**讲「官方怎么说、怎么加固到最优」

核心素材来自官方管理指南（Admin Guide）第 03 章基础命令、第 08 章进程管理、第 16 章 systemd 专章，以及 guides 下 systemd_hardening（systemd 单元加固）、firewalld（iptables→firewalld 迁移进阶）、learning_selinux（SELinux 官方指南）、dnf_package_manager（DNF 包管理器）四份指南。

**本轮最大增量是 systemd 加固的完整方法论**——用 `systemd-analyze security` 给服务暴露面打分（新装 httpd 默认 9.2 UNSAFE），再通过 capabilities 限制、文件系统只读、系统调用过滤三阶段把评分降到 1.5 OK，全过程有官方命令与评分对照。此外覆盖 firewalld 自定义 zone 迁移技巧、SELinux 官方排错三步法、DNF 仓库配置、进程优先级体系。

## 架构图

Rocky Linux 运维安全体系可抽象为**五层纵深**：最底层 DNF 包管理 → SELinux 强制访问控制 → firewalld 网络防火墙 → systemd 单元加固 → 最上层基础运维命令与进程管理。五层各自独立、层层收窄，任何一层被攻破都不会直接导致整机沦陷。

![[assets/linux-rocky/diagram-rocky-security-layers.svg]]

*图：Rocky Linux 五层纵深安全体系（自下而上：DNF → SELinux → firewalld → systemd 加固 → 基础命令/进程管理）*

## 核心概念

### systemd-analyze security 暴露面评分

systemd 内置安全审计工具，对每个 unit 逐项检查安全选项（RootDirectory、User=、CapabilityBoundingSet、NoNewPrivileges、UMask 等几十项），每项按未设置时的风险给 0.1-0.4 不等的 exposure 分值，最后汇总为 0-10 的 Overall exposure level 并给出 UNSAFE/OK/EXPOSED 表情评级。新装 httpd 默认 **9.2 UNSAFE**，是加固的起点度量。

> 💡 加固前先 `systemd-analyze security <unit>` 打分，每轮加固后重跑对比，用分数驱动而非凭感觉；官方全程对照：9.2 → 4.9 → 3.0 → 1.5。

### Linux capabilities 特权分解

自 Linux 2.2 起，root 的传统特权被拆分为 **41 种独立 capability**（如 CAP_CHOWN 任意改文件属主、CAP_KILL 绕过信号权限检查、CAP_NET_BIND_SERVICE 绑定 1024 以下特权端口），可按线程独立启用/禁用。分文件 capabilities（存于扩展属性，类似 suid，含 Permitted/Inheritable/Effective 三集合）与线程 capabilities 两类。

> 💡 `CapabilityBoundingSet=` 可裁剪服务进程的能力集——服务不需要的能力一律不授予，即使以 root 运行也受限于 bounding set。

### systemd 文件系统与内核限制组

- `ProtectSystem=strict`：整个文件系统只读挂载（/dev /proc /sys 除外，需配合 PrivateDevices=、ProtectKernelTunables=、ProtectControlGroups= 保护）
- `ProtectHome=true`：让 /home /root /run/user 不可达
- `PrivateDevices=true`：只留伪设备
- `ProtectKernelModules=`：禁止加载内核模块
- `ProtectProc=invisible`：隐藏他人进程；`ProcSubset=pid` 让 /proc 只留进程管理文件
- `NoExecPaths=/` 配合 `ExecPaths=`：限定可执行路径

> 💡 UMask 控制新建文件默认权限，官方示例：要 0640 权限可设 `UMask=7137`（对 7777 掩码运算得 640）；配 `ReadWritePaths=` 在白名单内重新开放可写路径。

### SystemCallFilter 系统调用过滤

限制进程可用的系统调用。精确做法：`strace -f -o httpd.strace` 跑一段时间，awk 提取调用名去重得 79 个，拼成 `SystemCallFilter=` 白名单；更稳的按组做法：`systemd-analyze syscall-filter` 查看预定义组（@system-service、@privileged、@resources、@mount、@swap、@reboot），`SystemCallFilter=@system-service` 配 `SystemCallFilter=~@privileged @resources @mount @swap @reboot` 排除危险组，`~` 前缀表示从允许集剔除。

> 💡 逐调用白名单最精确但漏一个就崩（官方演示 httpd 恰好仍工作）；生产建议先按组收窄再逐步细化，配合 `SystemCallArchitectures=native` 禁非本机架构 syscall。

**systemd 加固四阶段全程**（官方 httpd 案例，评分驱动）：

![[assets/linux-rocky/diagram-systemd-hardening-flow.svg]]

*图：systemd 单元加固四阶段流程（9.2 UNSAFE → capabilities 裁剪 → 文件系统限制 → 系统级限制 → 系统调用过滤 → 1.5 OK），每步 daemon-reload + restart 后重跑评分验收*

### firewalld 自定义 zone 与 target

firewalld 内置 drop/block/public/external/dmz/work/home/internal/trusted 等 zone。自定义 zone 用 `firewall-cmd --new-zone=admin --permanent` 创建，**关键陷阱**：新建 zone 的 target 默认是 default（不匹配任何规则就丢/拒），要当白名单用必须显式 `firewall-cmd --zone=admin --set-target=ACCEPT --permanent`。zone 只有绑定接口或 source IP 才 active。

**管理白名单 zone 全流程**：

![[assets/linux-rocky/diagram-firewalld-admin-zone.svg]]

*图：firewalld 管理白名单 zone 建立流程（new-zone → set-target=ACCEPT → add-source → add-service → 测试 → runtime-to-permanent → reload → 从 public 移除 ssh 收紧默认暴露）*

> 💡 管理白名单 zone 流程：`--new-zone` → `--set-target=ACCEPT` → `--add-source=管理IP` → `--add-service=ssh` → 测试 → `--runtime-to-permanent` → `--reload`；建好后从 public zone 移除 ssh 收紧默认暴露。

### SELinux 排错三步法（官方）

官方 learning_selinux 指南给出标准流程：

1. **Step 1**：`cat /var/log/audit/audit.log | grep AVC | grep denied | tail -1` 隔离最新拒绝记录
2. **Step 2**：用 `audit2why` 翻译原因（通常指向布尔开关或上下文标签）
3. **Step 3**：若需自定义策略，`audit2allow -M mylocalmodule` 生成 .te/.pp 模块，`semodule -i mylocalmodule.pp` 加载

> 💡 `audit2allow -m` 只生成 .te 源码，`-M` 才编译打包成 .pp；加载用 `semodule -i`；自定义上下文目录必须 `semanage fcontext -a` 写默认规则再 `restorecon`，否则会被恢复。

### DNF 仓库配置与优先级

DNF 配置集中在 `/etc/dnf/dnf.conf`：`[main]` 节设全局选项（gpgcheck=1、installonly_limit=3、clean_requirements_on_remove=True、best=True、skip_if_unavailable=False），可含多个 `[repository]` 节覆盖 main 的对应项；仓库文件在 `/etc/yum.repos.d/`。`dnf config-manager --dump` 输出全部配置键值（含 allow_vendor_change、cachedir=/var/cache/dnf 等）。

> 💡 `[repository]` 节的值优先于 `[main]`；每个用户（含 sudo）有独立缓存——`dnf clean all` 与 `sudo dnf clean all` 清理的不是同一个缓存；`dnf history list/undo` 可精确回滚事务。

### 进程优先级体系

Linux 进程分实时（优先级 0-99，实时调度算法）与普通（动态优先级 100-139，完全公平调度 CFS）；nice 值 -20 到 19 用于调整普通进程优先级，默认 0，**普通用户只能调高（0-19）不能调低**。`nice` 启动时设定（`nice -n 5 command`），`renice` 运行时调整（`renice -n 15 -p PID`）。

> 💡 nice 不带选项默认设 10；`pidof sleep | xargs renice -n 20` 一行改所有同名进程；`/etc/security/limits.conf` 可放宽每用户/每组的 nice 上限。

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方出处 |
|------|------|---------|---------|
| 新装 httpd 的 systemd-analyze security 评分 9.2 UNSAFE，不知从何下手加固 | 默认 unit 未配置任何限制：以 root 运行、能力无裁剪、文件系统可写、无系统调用过滤——41 种 capability 全开放 | 官方三阶段加固：① capabilities 阶段 `CapabilityBoundingSet=~CAP_SYS_TIME` 等裁剪；② 文件系统阶段 `UMask=7177` + `ProtectSystem=strict` + `ReadWritePaths=/run/httpd /etc/httpd/logs` + ProtectHome/PrivateDevices/ProtectKernelTunables/ProtectControlGroups/ProtectKernelModules/ProtectKernelLogs/ProtectProc=invisible/ProcSubset=pid + `NoExecPaths=/` + `ExecPaths=/usr/sbin/httpd /lib64` → 4.9 OK；③ 系统限制阶段 NoNewPrivileges/ProtectClock/SystemCallArchitectures=native/RestrictNamespaces/RestrictSUIDSGID/LockPersonality/RestrictRealtime/RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX/MemoryDenyWriteExecute/ProtectHostname → 3.0；④ `SystemCallFilter=@system-service` + `~@privileged @resources @mount @swap @reboot` → 1.5 OK。每步 daemon-reload + restart 后重跑评分 | [systemd_hardening](https://docs.rockylinux.org/guides/security/systemd_hardening/) |
| 服务以非特权用户运行后无法绑定 80/443 等特权端口（Permission denied: could not bind） | 端口 <1024 需要 CAP_NET_BIND_SERVICE，普通用户默认不具备该 capability | 用 setcap 给可执行文件授文件 capability：`setcap cap_net_bind_service=+ep /usr/sbin/httpd`（官方示例 sudo -u apache /usr/sbin/httpd 直接报 13 Permission denied）；注意文件 capabilities 不影响 unit 的 exposure 评分，属独立维度 | [systemd_hardening](https://docs.rockylinux.org/guides/security/systemd_hardening/) |
| firewalld 新建自定义 zone 后放行规则不生效，流量仍被丢 | 新建 zone 的 target 默认为 default（等价于拒绝未匹配流量），不显式改 target 就达不到白名单效果 | `firewall-cmd --zone=admin --set-target=ACCEPT --permanent`（REJECT/DROP 亦可按需），再 `--add-source` / `--add-service`，最后 `--reload`；官方强调 `--set-target` 必须带 `--permanent` 才生效 | [firewalld](https://docs.rockylinux.org/guides/security/firewalld/) |
| 想限制某网段能 ping 通服务器，但 ICMP 默认全开 | public/admin 等 zone 默认允许 ICMP echo-request/echo-reply，官方示例实验室场景需精确限定 ICMP 来源 | `firewall-cmd --zone=public --add-icmp-block={echo-request,echo-reply} --permanent` 逐 zone 阻断；花括号 `{}` 语法可一次传多个参数；改完 `--reload`，被禁 IP ping 得到 Packet filtered | [firewalld](https://docs.rockylinux.org/guides/security/firewalld/) |
| SELinux 拒绝服务但界面无任何报错，服务静默失败 | enforcing 模式下内核按策略直接拒绝，拒绝记录只写 audit.log（AVC denied），不打印到应用屏幕 | 官方三步：① `grep AVC /var/log/audit/audit.log | grep denied | tail -1` 隔离记录；② 同命令接 `audit2why` 翻译原因（通常指向布尔或上下文）；③ 需自定义策略时 `audit2allow -M mylocalmodule` 生成模块 + `semodule -i mylocalmodule.pp` 加载 | [learning_selinux](https://docs.rockylinux.org/guides/security/learning_selinux/) |
| semanage 命令找不到（command not found） | semanage 属 policycoreutils-python-utils 包，Rocky Linux 默认不安装 | `dnf provides */semanage` 定位所属包 → `sudo dnf install policycoreutils-python-utils`；安装后可用 `semanage boolean -l` / `semanage port -a -t http_port_t -p tcp 81` / `semanage fcontext -a` 等管理命令 | [learning_selinux](https://docs.rockylinux.org/guides/security/learning_selinux/) |
| dnf remove 一个包连带删除大量依赖（如删 perl 删 206 个包） | clean_requirements_on_remove=True（默认开启）会在卸载时自动移除不再被需要的依赖包 | 卸载前仔细审阅 Transaction Summary 的 Removing 列表；误删用 `dnf history list` 找事务 ID → `dnf history undo <ID>` 整体回滚；官方警告：重装同包可能遇到版本/旧版本冲突，回滚优先 | [dnf_package_manager](https://docs.rockylinux.org/guides/package_management/dnf_package_manager/) |
| 给服务加了 SystemCallFilter 白名单后服务崩溃或功能异常 | 逐调用白名单漏掉了服务某个运行路径需要的 syscall（官方用 strace 提取 httpd 得 79 个，任何遗漏都可能导致崩溃） | 优先用组集合：`SystemCallFilter=@system-service` 再 `SystemCallFilter=~@privileged @resources @mount @swap @reboot` 排除危险组；`systemd-analyze syscall-filter` 查看各组包含的调用；个别调用可用 `~` 前缀从允许集剔除 | [systemd_hardening](https://docs.rockylinux.org/guides/security/systemd_hardening/) |

## 最佳实践

| 实践 | 说明 | 官方来源 |
|------|------|---------|
| 加固用评分驱动，先度量再动手 | `systemd-analyze security <unit>` 给出 0-10 暴露分与逐项清单，作为加固的起点与每轮迭代的验收标准；官方示例 httpd 从 9.2 UNSAFE 经四阶段降到 1.5 OK，每一步都有评分对照，避免凭感觉配置 | [systemd_hardening](https://docs.rockylinux.org/guides/security/systemd_hardening/) |
| capabilities 最小化：bounding set 裁剪优先于完全放弃 root | `CapabilityBoundingSet=` 可排除 CAP_SYS_TIME 等不需要的能力，服务以 root 运行也受 bounding set 约束；41 种能力按需保留，无需为单个端口绑定引入 setcap | [systemd_hardening](https://docs.rockylinux.org/guides/security/systemd_hardening/) |
| firewalld 管理白名单 zone 模式 | 官方推荐：自定义 admin zone + set-target=ACCEPT + add-source 管理 IP，测试通过后 `--runtime-to-permanent` 固化，再从 public zone 移除 ssh 服务收紧默认暴露；**远程服务器绝不先删 ssh 服务**，否则锁死只能物理救援或重装 | [firewalld](https://docs.rockylinux.org/guides/security/firewalld/) |
| 远程改动防火墙遵守「先测试后固化」 | 规则先不带 `--permanent` 加进运行时，验证连通后再 `--runtime-to-permanent` + `--reload`；配错时重启即还原，避免远程锁死；ESTABLISHED,RELATED 放行由 firewalld 内部默认处理，无需手写 | [firewalld](https://docs.rockylinux.org/guides/security/firewalld/) |
| SELinux 保持 enforcing，排错走官方三步 | 禁用 SELinux 风险自负（官方原文 Warning）；遇拒绝先 audit2why 翻译，布尔能解决就 `setsebool -P`（带 -P 才持久化），上下文问题 `semanage fcontext` + `restorecon`，最后才考虑 audit2allow 自定义模块；临时排错用 permissive 而非 disabled | [learning_selinux](https://docs.rockylinux.org/guides/security/learning_selinux/) |
| DNF 事务可回滚，善用 history | `dnf history list` 查看事务流水，`dnf history undo <ID>` 精确回滚；删包前审阅依赖清单；注意用户级与 sudo 级缓存分离，clean 前确认用哪个身份 | [dnf_package_manager](https://docs.rockylinux.org/guides/package_management/dnf_package_manager/) |
| 进程管理：nice 只增不减，renice 运行时调整 | 普通用户只能调高 nice（0-19）不能调低；启动时 `nice -n 5 command`，运行中 `renice -n 15 -p PID`；`pidof` + `xargs` 批量调整；`/etc/security/limits.conf` 可放宽上限；跨发行版统一用 nice -n / renice -n 语法 | [Admin Guide 08-process](https://docs.rockylinux.org/books/admin_guide/08-process/) |
| 基础命令规范：man 手册分章节查阅 | man 手册 8 个章节（用户命令/系统调用/库函数/设备/文件格式/游戏/杂项/系统管理），`man [section] command` 精确查阅；`apropos` 按关键词搜手册，`whatis` 显示一行描述；短选项可组合（ls -lia），`--` 表示选项结束 | [Admin Guide 03-commands](https://docs.rockylinux.org/books/admin_guide/03-commands/) |

## 排查命令

```bash
# systemd 加固评分与调试
systemd-analyze security httpd                      # 暴露面评分（0-10，UNSAFE/OK）
systemd-analyze syscall-filter                      # 查看预定义 syscall 组
strace -f -o /tmp/httpd.strace -p $(pidof httpd)    # 采集实际系统调用
awk -F'[()]' '{print $2}' /tmp/httpd.strace | sort -u   # 提取调用名去重
systemctl daemon-reload && systemctl restart httpd  # 每轮加固后重载生效

# firewalld 管理白名单 zone
firewall-cmd --new-zone=admin --permanent
firewall-cmd --zone=admin --set-target=ACCEPT --permanent
firewall-cmd --zone=admin --add-source=<管理IP> --permanent
firewall-cmd --zone=admin --add-service=ssh --permanent
firewall-cmd --zone=public --add-icmp-block={echo-request,echo-reply} --permanent
firewall-cmd --reload
firewall-cmd --zone=admin --list-all                        # 核对 zone 配置

# SELinux 排错三步
grep AVC /var/log/audit/audit.log | grep denied | tail -1   # ① 隔离最新拒绝
grep AVC /var/log/audit/audit.log | grep denied | tail -1 | audit2why   # ② 翻译原因
audit2allow -M mylocalmodule && semodule -i mylocalmodule.pp   # ③ 自定义策略
setsebool -P httpd_can_network_connect on                    # 布尔开关（-P 持久化）
semanage fcontext -a -t httpd_sys_content_t '/web(/.*)?' && restorecon -Rv /web
semanage port -a -t http_port_t -p tcp 81                    # 自定义端口标签

# DNF 仓库与事务
dnf config-manager --dump                                    # 查看全部生效配置
dnf provides */semanage                                      # 定位命令所属包
dnf history list && dnf history undo <事务ID>                # 事务回滚
sudo dnf clean all                                           # 注意与用户级缓存分离

# 进程优先级
nice -n 5 <command>                                          # 启动时设定
renice -n 15 -p $(pidof <name>)                              # 运行时调整
pidof sleep | xargs renice -n 20                             # 批量调整同名进程
```

## 相关笔记

- [[rocky-linux-ops-basics]] — Rocky Linux 基础方法论篇（讲「是什么」：系统架构、基础操作）
- [[rocky-linux-ops-playbook]] — Rocky Linux 实战排错篇（讲「怎么排查」：常见故障与处理）
- 本篇为官方文档细节篇（讲「官方怎么说、怎么加固到最优」：systemd 加固评分驱动法、firewalld 白名单 zone、SELinux 官方三步、DNF 事务管理），三篇互补阅读

## 官方参考

- [Rocky Linux 官方文档：systemd 单元加固（systemd_hardening）](https://docs.rockylinux.org/guides/security/systemd_hardening/)
- [Rocky Linux 官方文档：firewalld 进阶（iptables 迁移指南）](https://docs.rockylinux.org/guides/security/firewalld/)
- [Rocky Linux 官方文档：SELinux 安全指南（learning_selinux）](https://docs.rockylinux.org/guides/security/learning_selinux/)
- [Rocky Linux 官方文档：DNF 包管理器指南](https://docs.rockylinux.org/guides/package_management/dnf_package_manager/)
- [Rocky Linux 官方管理指南第 16 章：systemd](https://docs.rockylinux.org/books/admin_guide/16-about-sytemd/)
- [Rocky Linux 官方管理指南第 08 章：进程管理](https://docs.rockylinux.org/books/admin_guide/08-process/)
- [Rocky Linux 官方管理指南第 03 章：Linux 用户基础命令](https://docs.rockylinux.org/books/admin_guide/03-commands/)
- [systemd.exec(5) 手册（加固指令权威参考）](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [capabilities(7) 手册（41 种能力完整清单）](https://man7.org/linux/man-pages/man7/capabilities.7.html)
