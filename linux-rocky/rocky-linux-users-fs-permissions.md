---
created: 2026-08-29
topic: RockyLinux
subtopic: 用户、文件系统与特殊权限管理
source: https://docs.rockylinux.org/books/admin_guide/
tags: [rocky-linux, users, lvm, acl, suid, sgid, sbit, chattr, sudo, filesystem]
---

# Rocky Linux 用户、文件系统与特殊权限管理 — useradd/LVM/ACL/SUID/chattr/sudo

## 概述

本轮 Rocky Linux 研究取「用户、文件系统与特殊权限管理」角度，素材全部来自官方文档 rocky-linux/documentation 仓库 books/admin_guide 三章：06-users（用户与组管理）、07-file-systems（分区/LVM/文件系统结构与权限）、14-special-authority（ACL/SUID/SGID/SBIT/chattr/sudo 特殊权限）。

核心要点：① 用户体系以 UID/GID 为内核身份标识，uid=0 即超级管理员，用户分 root(0)/系统用户(201-999)/普通用户(>=1000) 三类，管理必须用 useradd/usermod/userdel 等命令而非手改 /etc/passwd；② 标准分区容量固定无法在线调整，LVM 通过 PV→VG→LV 三层抽象（PE 默认 4MB）实现弹性扩容与快照；③ 文件系统由 Boot Sector/Super block/Inode table/Data block 四部分构成，inode 表大小决定文件数量上限，fsck 修复前必须卸载分区；④ 特殊权限体系解决基础 rwx 三身份不够用的问题——ACL 给指定用户/组单独授权（mask 为最大有效权限）、SUID/SGID 让普通用户临时获得属主/属组身份（passwd 改密码靠 SUID 写 000 权限的 /etc/shadow）、SBIT 防目录内互删（/tmp 1777）、chattr +i/+a 防误删防篡改、sudo 通过 /etc/sudoers 精确授权。

## 架构图

![[assets/linux-rocky/diagram-rocky-users-fs-arch.svg]]

*图：权限与存储管理三层架构——身份层（UID/GID 与账户文件）→ 存储层（分区/LVM/文件系统/挂载）→ 授权层（ACL/SUID/SBIT/chattr/sudo）*

## 核心概念

### UID/GID 与用户分类

内核仅识别 UID 与 GID 数字标识，uid=0 的用户即超级管理员（不一定是 root 这个登录名）。用户分三类：root(uid=0)、系统用户/伪用户(uid 201-999，供服务与进程管理权限)、普通用户(uid>=1000)。每个用户有一个主组（primary group）和若干附属组（supplementary groups）。

- GID 范围由 /etc/login.defs 的 SYS_GID_MIN/MAX 与 GID_MIN/MAX 定义
- 管理用户必须用命令而非手工编辑文件（官方 Danger 警告）

### 用户/组管理命令族

groupadd/groupmod/groupdel 管理组；useradd/usermod/userdel 管理用户；gpasswd/id/newgrp 管理组成员与临时切换主组；passwd/chage 管理密码与期限。useradd 默认创建同名主组+家目录+/bin/bash，新建账户无密码且锁定；usermod -l 改登录名后需同步改家目录名；userdel -r 连家目录与邮件 spool 一并删除（USERGROUPS_ENAB yes 时连主组也删）。

- 改 UID/GID 后旧属主文件变未知数字，需 `find / -uid 旧UID -exec chown 新UID: {} \;` 重新归属
- newgrp 切换主组会生成子 shell（SHLVL+1）

### /etc/shadow 密码策略九字段

第 2 字段为 SHA512 加密哈希（ENCRYPT_METHOD 定义）；第 3 字段密码最后修改时间（1970-01-01 起的天数时间戳）；4/5/6/7 字段为最小生存期、最大生存期、过期前警告天数、过期后宽限天数；第 8 字段账户过期时间。账户过期与密码过期不同：账户过期后完全禁止登录，密码过期只是不能再用密码登录。

- chage -m/-M/-W/-I/-E 一键管理各期限
- passwd -l 在 shadow 密码字段前加 !! 锁账户
- `date -d "1970-01-01 N days"` 换算时间戳

### 磁盘分区与设备命名

设备文件命名：IDE /dev/hd[a-d]、SCSI/SATA/USB /dev/sd[a-z]、虚拟磁盘 /dev/vd[a-z]（KVM 等虚拟化）、光驱 /dev/sr0；分区号跟在设备名后（MBR 下第一个逻辑分区必须是 5）。MBR 表最多 4 主分区或 3 主+1 扩展（扩展分区只能含逻辑分区），最大识别 2TB；fdisk 不支持 GPT，cfdisk 更可靠，大磁盘用 parted。

- MBR 单盘超 2TB 无法处理
- gparted 是图形化分区工具，parted 带分区表恢复能力

### LVM 逻辑卷管理

LVM 在物理磁盘与文件系统之间加抽象层：PV（物理卷，磁盘/分区/RAID 初始化而来）→ VG（卷组，多个 PV 汇聚）→ LV（逻辑卷，在 VG 上按 PE 分配，PE 默认 4MB）。支持在线数据迁移、stripe 条带、镜像、快照。命令对照：pvcreate/vgcreate/lvcreate、pvdisplay/vgdisplay/lvdisplay、vgextend/lvextend 扩容、vgreduce/lvreduce 缩容、pvs/vgs/lvs 汇总。

![[assets/linux-rocky/diagram-lvm-pv-vg-lv.svg]]

*图：LVM 创建流程（pvcreate→vgcreate→lvcreate→mkfs→挂载）与在线扩容流程（vgextend→lvextend→xfs_growfs）*

- /dev/VG/LV 是软链接指向 /dev/dm-*
- lvcreate -l 可指定 PE 数或百分比，-L 指定 K/M/G 大小
- PV/VG/LV 四层（含 mkfs+mount）准备顺序不可颠倒

### 文件系统内部结构

每个文件系统（swap 除外）结构统一：Boot Sector（MBR 446 字节引导加载器 + DPT 64 字节分区表 + BRID 2 字节引导标识）、Super block（FS 元数据：类型/大小/空闲块数/inode 表大小）、Inode table（每文件一个 inode：权限/属主/时间戳/数据块指针，inode 号在 FS 内唯一）、Data block（目录项与文件内容）。superblock 与 inode 表在内存有副本，经 sync 定期落盘，突然断电会丢失一致性。

- inode 表大小决定 FS 可容纳最大文件数
- fsck 检查一致性需先卸载分区（根分区用 `touch /forcefsck` + reboot）

### 挂载与 /etc/fstab

fstab 每行 6 字段：设备（/dev/sda1 或 UUID）/ 挂载点（绝对路径，swap 除外）/ 文件系统类型 / 挂载选项 / dump 标志 / fsck 顺序（1 根、2 其他、0 不检查）。mount -a 挂载 fstab 全部条目；defaults = rw,suid,dev,exec,auto,nouser,async。mount/umount 的 -n 选项不写 /etc/mtab。

- umount 报 device is busy 是因为当前 shell 停留在挂载点目录下，cd 出去即可
- umount -r 失败时自动改只读重挂

### ACL 访问控制列表

ACL 解决 Linux 三身份（owner/group/other）无法满足「给指定单个用户/组授权」的问题。setfacl -m u:user:perm 增加条目、-x 删除、-b 清除全部扩展 ACL、-d 设置默认 ACL（新文件继承）、-R 递归（仅作用于已存在文件）。带 ACL 的文件 ls -l 权限位后出现 + 号。

- mask 是最大有效权限：用户实际权限 = 用户 ACL 权限与 mask 的逻辑与（getfacl 中 #effective 标注）
- 默认 ACL 与递归必须作用在目录上，作用文件会报错

### SUID/SGID/SBIT 特殊权限

SUID(4，数字位 4755 或 u+s)：仅可执行二进制，执行期间临时获得文件属主身份——passwd 命令靠它让普通用户写入权限 000 的 /etc/shadow；SGID(2，2711 或 g+s)：二进制执行期间临时获得属组身份（locate 靠它读 slocate 组的 mlocate.db），目录加 SGID 后新文件继承目录属组；SBIT(1，1777 或 o+t)：仅目录，普通用户只能删自己的文件（/tmp 即为 1777）。属主/属组无 x 时显示大写 S/T 表示特殊位无效。

![[assets/linux-rocky/diagram-special-authority-flow.svg]]

*图：特殊权限选型决策流——ACL 授权、SUID/SGID 提权、SBIT 防互删、chattr 防篡改、sudo 命令授权*

- SUID/SGID 是提权后门高危点：`find / -perm -4000 -type f` 与 `find / -perm -2000 -type f` 定期扫描
- root(uid=0) 不受特殊权限限制

### chattr 不可变属性

chattr +i 设置 immutable：文件禁删除/禁修改/禁追加（只能查看），目录禁删除目录本身与其中文件、禁新建文件（但目录内已有文件可改可追加）；chattr +a 仅追加：文件只能 append（日志场景），目录内可新建文件但不可删改已有文件；lsattr 查看属性，chattr -i/-a 移除。

- ia 同设时文件除查看外什么都做不了
- +i 是保护 /etc/passwd 等关键文件防意外删除的标准手段

### sudo 与 /etc/sudoers

sudo 由 root 将 /sbin、/usr/sbin 等仅 root 可执行的命令授权给普通用户。visudo 编辑 /etc/sudoers（带语法检查，普通编辑器写错会锁死 sudo）；语法 user MACHINE=(RUNAS) COMMANDS，如 root ALL=(ALL) ALL；secure_path 限定 PATH；sudo -l 查看当前用户可用命令；visudo -c 校验语法。

- 授权 /sbin/shutdown 即允许其任意选项
- sudo 历史高危漏洞 CVE-2019-14287、CVE-2021-3156（Baron Samedit）、CVE-2025-32462/32463，可换 Rust 实现的 sudo-rs 替代

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方出处 |
|------|------|---------|---------|
| groupdel 删除组时报 cannot remove the primary group of user | 目标组仍是某个用户的唯一主组（primary group），系统禁止删除 | 先把该用户的主组改到别的组再删：`sudo usermod -g users -G test test`，该组降为附属组后 groupdel 即可删除 | [06-users](https://docs.rockylinux.org/books/admin_guide/06-users/) |
| usermod -u / groupmod -g 修改 UID/GID 后，文件属主显示为数字而非用户名 | 文件仍记录旧 UID/GID，新标识下无对应名称映射 | 用 find 批量重新归属：`sudo find / -uid 1000 -exec chown 1044: {} \;`（改 UID）；`sudo find / -gid 1002 -exec chgrp 1016 {} \;`（改 GID）；修改前需先退出登录且无运行中进程 | [06-users](https://docs.rockylinux.org/books/admin_guide/06-users/) |
| setfacl -R 递归设置 ACL 后，目录里新建的文件没有 ACL 权限 | -R 递归只作用于设置时已存在的文件；新文件不继承 ACL，除非配置默认 ACL | 对目录设置默认 ACL：`setfacl -m d:u:tom:rx /project`，此后新建文件自动继承（getfacl 输出出现 default: 段）；-d 与 -R 都必须在目录上操作 | [14-special-authority](https://docs.rockylinux.org/books/admin_guide/14-special-authority/) |
| umount 卸载分区报 device is busy | 当前 shell 工作目录停留在挂载点或其子目录内，文件系统仍被进程占用 | cd 离开挂载点（如 cd /）后重试 umount；确需强制可用 umount -f，失败自动降级只读可加 -r，延迟卸载用 umount -l | [07-file-systems](https://docs.rockylinux.org/books/admin_guide/07-file-systems/) |
| 普通用户无法删除 /tmp 下他人创建的文件（Operation not permitted） | /tmp 目录权限为 1777（drwxrwxrwt），SBIT 只允许用户删除自己创建的文件——预期保护行为，非故障 | 属主自己可删：rm /tmp/自己的文件；root(uid=0) 不受 SBIT 限制；若确需共享目录互删，不加 SBIT 或用组协作目录（配合 SGID） | [14-special-authority](https://docs.rockylinux.org/books/admin_guide/14-special-authority/) |
| chattr +i 后文件无法修改、追加甚至删除（Operation not permitted） | immutable 属性使文件完全只读，root 也无法绕过 | 先移除属性再操作：`chattr -i 文件`；目录 +i 时目录本身与其中文件均禁删、目录内禁新建文件（但已有文件可改）；查看属性用 lsattr | [14-special-authority](https://docs.rockylinux.org/books/admin_guide/14-special-authority/) |
| 用户密码过期与账户过期的表现混淆 | /etc/shadow 第 5 字段（密码最大生存期，chage -M）控制密码过期——过期后只是不能用密码登录；第 8 字段（账户过期日，chage -E）控制账户过期——过期后完全禁止登录 | 密码过期：`chage -M 90 -W 7` 设置 90 天有效期+7 天警告，用户改密即可恢复；账户过期：`chage -E 2026-12-31` 设定硬性禁用日；查明细用 chage -l 用户名 | [06-users](https://docs.rockylinux.org/books/admin_guide/06-users/) |
| 新建用户无法登录 | useradd 创建的账户默认无密码且处于锁定状态（设计行为，防止未设密账户直接登录） | 管理员为其设置密码解锁：`sudo passwd 用户名`；脚本批量建户可用 chpasswd 或 `echo 密码 \| passwd --stdin`；需强制首次登录改密可用 `chage -d 0 用户名` | [06-users](https://docs.rockylinux.org/books/admin_guide/06-users/) |
| 手工编辑 /etc/sudoers 语法错误导致所有用户 sudo 失效 | sudoers 语法敏感，直接 vim 编辑写入错误行后 sudo 全线拒绝执行 | 必须用 visudo 编辑（保存时自带语法检查，出错拒绝保存）；已写坏时用 pkexec visudo 修复；例行校验用 visudo -c；普通用户查看自己被授权范围用 sudo -l | [14-special-authority](https://docs.rockylinux.org/books/admin_guide/14-special-authority/) |
| fsck 修复文件系统时报文件系统正忙或拒绝执行 | fsck 要求目标分区处于卸载状态，挂载中的文件系统（尤其是根分区）无法安全检查 | 普通分区先 umount 再 `fsck /dev/sdaX`；根分区用 `touch /forcefsck` 后 reboot，或 `shutdown -r -F now`；修复后无 inode 条目的文件会被归入 /lost+found | [07-file-systems](https://docs.rockylinux.org/books/admin_guide/07-file-systems/) |

## 最佳实践

1. **用户管理一律用命令而非手工编辑文件** — 官方 Danger 警告：/etc/passwd、/etc/shadow、/etc/group、/etc/gshadow 必须通过 useradd/usermod/userdel、groupadd/groupmod/groupdel、passwd 等管理命令修改，直接编辑易导致字段错位、passwd 与 shadow 行不对应等破坏性错误；每个 passwd 行必须有对应 shadow 行。（[06-users](https://docs.rockylinux.org/books/admin_guide/06-users/)）
2. **修改 UID/GID 后必须重新归属文件** — usermod -u / groupmod -g 改变标识后，原属主文件会变为未知数字，官方示例用 `find / -uid 旧UID -exec chown 新UID: {} \;` 与 `find / -gid 旧GID -exec chgrp 新GID {} \;` 全盘重新归属，避免文件权限悬空。（[06-users](https://docs.rockylinux.org/books/admin_guide/06-users/)）
3. **服务器磁盘优先采用 LVM 而非标准分区** — 标准分区挂载后容量固定、强行扩缩易丢数据；LVM 支持在线扩容（vgextend+lvextend）、条带、镜像与快照，官方文档明确 LVM 是服务器弹性存储的推荐方案，虚拟化环境甚至可直接整盘 pvcreate 便于后续扩盘。（[07-file-systems](https://docs.rockylinux.org/books/admin_guide/07-file-systems/)）
4. **定期扫描 SUID/SGID 文件排查提权风险** — SUID 可让普通用户临时获得属主（常为 root）身份，是提权后门与误配置的高危点；官方建议用 `find / -perm -4000 -type f -exec ls -l {} \;` 与 `find / -perm -2000 -type f -exec ls -l {} \;` 定期排查，发现异常文件立即审计处置。（[14-special-authority](https://docs.rockylinux.org/books/admin_guide/14-special-authority/)）
5. **关键配置文件用 chattr +i 防意外删除与篡改** — 对 /etc/passwd 等关键文件设置 immutable 属性可防止误删误改（root 亦无法绕过，需先 chattr -i）；日志类文件用 chattr +a 仅追加模式，保证只能 append 不能改写历史记录。（[14-special-authority](https://docs.rockylinux.org/books/admin_guide/14-special-authority/)）
6. **文件名避免空格，用下划线替代** — 官方明确 best practice：虽然技术上允许创建含空格的文件/目录，但应避免并在必要时用下划线替换——空格文件名在脚本、管道与引号处理中极易出错。（[07-file-systems](https://docs.rockylinux.org/books/admin_guide/07-file-systems/)）
7. **目录权限 r 与 x 通常成对出现** — 目录的 r 只允许列出内容（ls），进入目录（cd）需要 x；只有 r 没有 x 时无法进入目录，只有 x 没有 r 时无法列出——官方建议 r 和 x 一起设置，避免半可用状态。（[07-file-systems](https://docs.rockylinux.org/books/admin_guide/07-file-systems/)）
8. **sudo 授权用 visudo 并保持最小权限** — 编辑 /etc/sudoers 必须走 visudo（自带语法校验）；授权按最小权限原则只给具体命令而非 ALL；授权 /sbin/shutdown 即等于允许其全部选项；sudo 本身历史漏洞多（CVE-2021-3156 等），官方提示可评估 Rust 版 sudo-rs 替代。（[14-special-authority](https://docs.rockylinux.org/books/admin_guide/14-special-authority/)）

## 排查命令

```bash
# 用户与组
id 用户名                 # 查看 UID/GID 与所属组
getent passwd 用户名      # 从 NSS 查询用户信息
chage -l 用户名           # 查看密码期限明细
find / -uid 1000 -exec chown 1044: {} \;   # 改 UID 后重新归属文件

# LVM 与文件系统
pvs && vgs && lvs         # 三层汇总信息
pvdisplay / vgdisplay / lvdisplay   # 详细视图
vgextend vg0 /dev/sdc     # 扩容卷组
lvextend -L +10G /dev/vg0/lv0 && xfs_growfs /mountpoint   # 在线扩容
df -h && mount -a         # 挂载状态与重挂 fstab
fsck /dev/sdaX            # 卸载后检查文件系统
touch /forcefsck && reboot   # 根分区强制检查

# ACL
getfacl 文件/目录
setfacl -m u:tom:rx /project
setfacl -m d:u:tom:rx /project   # 默认 ACL（新文件继承）
setfacl -b /project              # 清除全部扩展 ACL

# 特殊权限
ls -l /usr/bin/passwd /tmp    # 观察 SUID/SBIT 位（rws / rwt）
find / -perm -4000 -type f    # 扫描 SUID 文件
find / -perm -2000 -type f    # 扫描 SGID 文件
chattr +i /etc/passwd && lsattr /etc/passwd   # 设置/查看 immutable

# sudo
visudo -c                     # 校验 sudoers 语法
sudo -l                       # 查看当前用户可用命令
pkexec visudo                 # sudoers 写坏后的修复入口
```

## 相关笔记

- [[rocky-linux-ops-basics]] — Rocky Linux 基础运维命令、systemd、firewalld、SELinux、dnf（入门全景）
- [[rocky-linux-security-hardening]] — 安全加固：systemd-analyze 评分、capabilities、SystemCallFilter、SELinux 排错
- [[rocky-linux-logging-scheduling]] — 日志管理与自动化运维：rsyslog/journald/logrotate、cron/anacron

## 参考资料

- [Rocky Linux Admin Guide — User Management（用户与组管理）](https://docs.rockylinux.org/books/admin_guide/06-users/)
- [Rocky Linux Admin Guide — File System（分区/LVM/文件系统结构与权限）](https://docs.rockylinux.org/books/admin_guide/07-file-systems/)
- [Rocky Linux Admin Guide — Special Authority（ACL/SUID/SGID/SBIT/chattr/sudo）](https://docs.rockylinux.org/books/admin_guide/14-special-authority/)
- [rocky-linux/documentation 官方文档仓库](https://github.com/rocky-linux/documentation)
- [sudo-rs — Rust 版 sudo 替代实现](https://github.com/trifectatechfoundation/sudo-rs)
