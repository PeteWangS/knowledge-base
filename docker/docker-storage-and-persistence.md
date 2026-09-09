---
created: 2026-09-09
tags: [docker, storage, storage-driver, overlay2, copy-up, volumes, bind-mount, tmpfs, image-mount, containerd-image-store]
source: Docker 官方文档
---

# Docker 镜像分层存储与容器数据持久化（存储驱动 / 卷 / bind / tmpfs / image mounts / containerd image store）

## 概述

本篇为 Docker 主题**存储侧第一篇**。前五篇（[[dockerfile-best-practices]] 2026-07-29、[[docker-buildkit-cache-supplychain-security]] 2026-08-11、[[docker-image-optimization-security]] 2026-08-19、[[docker-advanced-compose-swarm-security]] 2026-07-27、[[docker-compose-swarm-security-deep-dive]] 2026-08-10）全部聚焦**镜像构建与编排侧**，本轮补上存储体系空白——两条主线：

1. **镜像与容器如何落盘**：镜像分层模型（每指令一层、层是相对前一层的 diff）、存储驱动（overlay2 经典驱动 / containerd snapshotter）、overlay2 的 on-disk 四元组（lowerdir / upperdir / merged / work）与 CoW 机制（copy_up / whiteout / opaque）
2. **持久化数据如何挂载进容器**：四通道对比——卷 volumes（官方首选）、bind mount、tmpfs、image mount，以及存储后端演进（Engine 29.0+ 全新安装默认 containerd image store）

核心结论：镜像层**只读共享**、可写层**每容器独有且随容器销毁**、写密集与需持久化的数据**必须放卷**（绕过存储驱动直写宿主机文件系统）。素材全部取自 Docker 官方文档 `engine/storage` 全套 9 份（storage 总览 / drivers / overlayfs / select-storage-driver / volumes / bind-mounts / tmpfs / containerd / image-mounts）。

---

## 架构图

### Docker 存储体系全景

![[assets/docker/diagram-docker-storage-arch.svg]]

*图：Docker 存储体系全景——Dockerfile 指令逐层构建只读镜像栈 → 存储驱动以 overlay2 四元组（lowerdir 镜像层 + upperdir 容器可写层 + merged 联合视图 + work）落盘 → 容器进程读写 merged 视图，写落在可写层（CoW）→ 持久化数据经卷 / bind / tmpfs / image mount 四通道挂载，绕过存储驱动；存储后端由 overlay2 经典驱动向 containerd image store 演进。*

---

## 核心概念

### 1. 镜像层与可写容器层（Images and layers）

镜像由多层**只读层**叠加而成，每个改动文件系统的 Dockerfile 指令（FROM / COPY / RUN 等）生成一层；LABEL / CMD / EXPOSE 等只改元数据的指令**不产层**。层是相对前一层的 diff 集合——删除文件也会产生新层（whiteout），被删内容仍留在下层占空间。容器启动时在镜像层之上叠加一层**薄可写层**：所有写入（增/改/删）都落在可写层，容器删除即可写层销毁，底层镜像不变，多个容器可共享同一镜像及其公共层。

> **关键点**：可写层每容器独有且随容器销毁，不适合放需持久化或共享的数据；镜像层被多容器共享，公共基础镜像的层在磁盘上只存一份。

### 2. 存储驱动与写时复制（Storage drivers & CoW）

存储驱动管理镜像层与可写层的落盘实现：**overlay2**（经典驱动默认，文件级 CoW，兼容性与稳定性最佳，官方推荐）、**containerd snapshotter**（Engine 29.0+ 全新安装默认）、btrfs/zfs（块级 CoW、写密集更好但吃内存）、vfs（测试用、无 CoW）。写入容器可写层有存储驱动的额外抽象开销，性能低于卷直写宿主机文件系统。

### 3. copy_up / whiteout / opaque（overlay2 读写机制）

overlay2 容器运行时生成 **lowerdir**（镜像层，只读）+ **upperdir**（容器可写层）+ **merged**（联合视图）+ **work**（内部工作目录）四元组，镜像层按序存入 `/var/lib/docker/overlay2`（每层一个目录 + `l/` 短链接目录规避 mount 参数页长限制）。读写机制：

![[assets/docker/diagram-docker-copyup-whiteout-flow.svg]]

*图：overlay2 文件操作流程——首次写某文件时 copy_up 把整文件从 lowerdir 复制到 upperdir 再修改（镜像下层不变）；删除文件在 upperdir 生成 whiteout 遮罩、删除目录生成 opaque 遮罩（下层实体仍在）；容器删除即可写层销毁、镜像层保留。*

> **关键点**：文件级 CoW——改 1 字节也拷全文件，大文件首写有延迟；chmod / chown 等元数据变更同样触发 copy_up；跨层 rename 返回 EXDEV 需 copy+unlink 兜底。这也是「容器里删大文件但磁盘空间没释放」的根因。

### 4. 卷（Volumes）

由 Docker daemon 管理的持久化存储，数据在宿主机 `/var/lib/docker/volumes/<name>/_data`，**生命周期独立于容器**。分**命名卷**与**匿名卷**（随机名，`--rm` 启动的容器其匿名卷随容器删除）。**空卷首次挂载会把容器目录既有内容自动拷贝进卷**（`volume-nocopy` 可关闭）；非空卷挂载则纯遮蔽（见常见问题表）。

> **关键点**：官方首选持久化方案——易备份迁移（官方 tar 流程）、可多容器共享、绕过存储驱动性能最好；bind propagation 固定 `rprivate` 不可配置。

### 5. bind mount

把宿主机任意路径直接挂进容器，容器内外**双向可见可改**。语法差异是高频坑：`-v` 源路径不存在会自动以 root 建目录（恒为目录）；`--mount type=bind` 则直接报错，需加 `bind-create-src` 选项显式创建。

> **关键点**：强依赖宿主目录结构、跨主机不可移植；容器进程可借此修改宿主机文件，有安全面——**能用卷就不用 bind**；SELinux 发行版（RHEL/CentOS/Rocky）需 `:z`（共享）或 `:Z`（私有独占）重标标签。

### 6. tmpfs 与 image mount

**tmpfs**：数据放宿主机内存不落盘，容器停止/重启即失，仅 Linux 可用、不可跨容器共享；数据**计入容器内存限制**，size 不提高上限（默认上限为宿主机内存 50%）。**image mount**：把另一镜像的只读内容挂进容器（调试无 shell 的硬化镜像、共享只读资产），需要 containerd image store 且不自动拉取源镜像；只支持 `--mount` 语法，动态链接二进制要求运行时兼容。

### 7. containerd image store（存储后端演进）

用 containerd snapshotter 替代经典 graph driver 管理镜像存储：支持本地**多平台镜像**、attestation（provenance/SBOM）、Wasm 负载与 stargz/nydus 等高级 snapshotter。代价：镜像**同时存压缩层与解压层**，磁盘占用比经典驱动大；切换后端会暂时隐藏旧后端镜像与容器（数据仍在盘上，回切配置即恢复）；containerd 存储路径独立，**不跟随自定义 docker data-root**，需单独配置。

---

## 常见问题表

| 问题 | 原因 | 解决方案 | 官方出处 |
|------|------|----------|----------|
| 容器删除后数据全部丢失，或想把容器里产生的文件拷出来很困难 | 数据默认写在容器可写层（thin writable layer），容器删除时该层一并销毁；可写层内容每容器独有、不易直接访问或提取 | 需持久化/共享/高性能写入的数据用卷或 bind mount 保存；仅临时状态才写容器层；非持久状态数据用 tmpfs 避免落盘并提升性能 | storage/drivers — Storage drivers versus Docker volumes |
| 挂载卷后容器镜像自带的文件「不见了」（配置目录被掏空、应用行为异常） | 任何挂载（卷/bind/tmpfs/image mount）都会遮蔽目标目录中既有文件，容器内无法卸载挂载还原；只有空卷首次挂载才会把容器目录内容拷贝进卷 | 想恢复镜像原内容只能重建不带挂载的容器；想让镜像内容进卷，确保卷为空（新卷自动预填充）或用 populate 容器显式拷贝；禁止自动拷贝用 volume-nocopy；bind/tmpfs 挂载无拷贝行为、纯遮蔽 | volumes — Mounting a volume over existing data |
| `docker run -v` 挂载不存在的宿主机路径后容器里是空目录，或 `--mount` 直接启动失败 | `-v` 遇到不存在的源路径自动以 root 创建目录（恒为目录）；`--mount type=bind` 不自动创建，报 `bind source path does not exist` | 宿主机目录先 mkdir 再启动；或 `--mount` 加 `bind-create-src` 让 Docker 自动建源目录；挂载单个文件时源文件必须预先存在 | bind-mounts — Syntax |
| 把宿主机目录 bind 到容器系统目录（如 /usr）后容器启动报 exec not found | bind mount 遮蔽镜像内原有可执行文件路径，OCI runtime 启动进程时找不到程序（示例：`-v /tmp:/usr` 后 nginx 报 executable file not found） | 只向容器内独立空目录挂载；避免 bind 覆盖镜像自带内容的目录；挂载 /usr、/bin 等系统路径会造出不可用容器 | bind-mounts — Mount into a non-empty directory on the container |
| 启用 SELinux 的发行版（RHEL/CentOS/Rocky）上容器读不到 bind mount 文件，报 Permission denied/AVC 拒绝 | 宿主机文件的 SELinux 标签与容器 domain 不匹配，容器进程无访问权 | 用 `-v host:/path:z`（多容器共享）或 `:Z`（私有独占）让 Docker 重标宿主机文件；`:Z` 用于 /home、/usr 等系统目录会致宿主机不可用需手工 relabel，务必谨慎；`--mount` 语法不支持 z/Z 只能走 `-v`；Swarm service 场景 :z/:Z/:ro 均被忽略（moby/moby#32579） | bind-mounts — Configure the SELinux label |
| tmpfs 数据容器重启就丢；tmpfs 写太多把容器搞 OOM | tmpfs 存于宿主机内存且不落盘，容器停止/重启/宿主机重启即失；数据计入容器内存限制，size 参数不提高该上限（默认上限为宿主机内存 50%）；tmpfs 不可跨容器共享、仅 Linux 可用 | 仅对临时/敏感数据（凭据、缓存、中间产物）用 tmpfs；用 `--mount type=tmpfs,dst=...,tmpfs-size=...,tmpfs-mode=...` 精确设大小与权限（默认 mode 1777）；tmpfs 权限在容器重启后会重置（docker/for-linux#138），必要时显式设 uid/gid 规避 | tmpfs — Limitations of tmpfs mounts |
| 容器里删了大文件但磁盘空间没释放；镜像体积怎么都减不下来 | 容器内删除只生成 whiteout/opaque 遮罩，文件实体仍留在只读镜像下层；Dockerfile 里 RUN rm 同样只新增一层、被删内容仍在旧层累计进镜像总大小；容器删除只释放可写层 | 镜像瘦身靠多阶段构建、合并 RUN、精简 COPY 内容（官方 best-practices/multi-stage）；用 `docker image prune` 清理悬空镜像、`docker system df` 监控磁盘；大文件不要写容器层 | storage/drivers — Images and layers |
| overlay2 下首次写大文件很慢，写密集应用（如数据库）跑在容器里性能差 | CoW copy_up 在文件首次被写时把整个文件从镜像层复制到可写层（OverlayFS 是文件级而非块级，改 1 字节也拷全文件）；chmod/chown 等元数据变更同样触发 copy_up；写容器层有存储驱动抽象开销 | 写密集负载用卷——绕过存储驱动直写宿主机文件系统，性能最好且可预测；大文件首次写延迟属设计行为，之后写同一文件不再 copy_up；SSD 可显著改善；不要依赖容器可写层跑数据库 | storage/drivers/overlayfs — copy_up |
| 切换存储驱动或启用 containerd image store 后所有镜像与容器「消失」 | 不同存储后端层格式不兼容：切换后旧镜像/容器不可访问（数据仍在磁盘，回切配置即恢复可见）；切换期间新建的镜像同样会随回切而隐藏 | 切换前用 `docker save` 导出或 push 到 registry 备份；改 daemon.json 的 storage-driver 或 features.containerd-snapshotter 后 `systemctl restart docker`，用 `docker info` 的 Storage Driver/DriverStatus 验证；需要保留旧镜像时先推送或 save 再迁移 | select-storage-driver — Check your current storage driver |
| 启用 containerd image store 后磁盘占用明显变大，自定义 docker 数据目录不生效 | containerd 同时保留压缩层（registry 传输格式）与解压层，经典驱动只存解压层，同镜像占盘更大；containerd 存储路径独立于 docker data-root，之前自定义的数据目录不会自动迁移，可能悄悄撑爆根分区 | 定期 `docker image prune`、用 `docker system df` 监控；单独为 containerd 配置数据目录指向大分区；仅在需要多平台镜像/attestation/Wasm/惰性拉取时切换，普通场景 overlay2 更省盘；切换前旧镜像 push 或 `docker save` 保留 | storage/containerd — Disk space usage |

---

## 最佳实践

选型决策总览：

![[assets/docker/diagram-docker-persistence-choice-flow.svg]]

*图：数据放置选型决策流——需要持久化？否 → 临时/敏感用 tmpfs、其余写可写层；是 → 需宿主机双向访问用 bind mount、只读复用他镜像用 image mount、默认用卷（官方首选）。*

### 1. 数据持久化优先用卷，别写容器可写层

卷由 daemon 管理、生命周期独立于容器、可多容器共享、易备份迁移，且直写宿主机文件系统**绕过存储驱动**（无 CoW/精简置备开销）；写密集应用（数据库、日志量大）尤其应放卷；卷还不会增大容器尺寸。

### 2. 能卷则卷，bind mount 仅当需要宿主机双向访问

bind mount 依赖宿主机目录结构、跨主机不可移植，且容器内进程默认可写、能增删改宿主机文件（有安全影响）；需要 host 与容器同时访问（源码开发、配置文件注入）才用 bind，并用 `ro`/`readonly` 收窄写权限。

### 3. 卷备份/迁移用官方 tar 流程

备份：`docker run --rm --volumes-from <旧容器> -v $(pwd):/backup ubuntu tar cvf /backup/backup.tar /数据目录`；还原：`tar xvf /backup/backup.tar --strip 1` 解到新容器卷内；可脚本化实现自动化备份与跨主机迁移。

### 4. 优先 --mount 而非 -v

`--mount` 更显式且支持全部选项：卷驱动（volume-driver/volume-opt）、卷子目录挂载（volume-subpath，子目录必须预先存在否则挂载失败）、bind 自动建源目录（bind-create-src）、递归只读（bind-recursive）、tmpfs 大小/模式；`-v` 只支持子集（ro、z/Z、传播模式、volume-nocopy）。

### 5. 只读挂载与递归只读收紧权限边界

`ro`/`readonly` 挂载防止容器改数据；`bind-recursive=readonly` 把子挂载也置只读，但要求内核 5.12+（旧内核子挂载自动变 rw，且显式 readonly 会报错）；传播默认 rprivate，bind propagation 仅 Linux 可配且 Docker Desktop 不支持。

### 6. tmpfs 只放临时/敏感数据

凭据、缓存、中间产物等无需落盘的数据用 tmpfs 提升性能并避免写盘痕迹；注意其计入容器内存限制，size 用 `--mount tmpfs-size=` 精确控制；Swarm service 不能用 `--tmpfs` 标志，须 `--mount type=tmpfs`；匿名卷清理用 `docker run --rm`。

### 7. 镜像层设计：每指令一层，删文件不减体积

每个改动文件系统的指令产生一层，RUN rm 只是新增一层、被删内容仍占镜像体积；用多阶段构建、合并 RUN、精简 COPY 上下文控制层数与体积；公共基础镜像的层被多镜像共享，`docker image history` 可核对层 ID。

### 8. 切换存储后端前先备份，升级注意默认值变化

改 storage-driver 或启用 containerd image store 前，先 `docker save` 或 push 全部镜像；Engine 29.0+ 全新安装默认 containerd image store（snapshotter），旧版升级仍沿用 overlay2 需手动迁移；迁移前确认 containerd 独立数据目录落在足够大的分区。

---

## 排查命令

```bash
# 查看当前存储驱动与 DriverStatus（overlay2 / containerd snapshotter）
docker info --format '{{.Driver}}'
docker info | grep -A 6 "Storage Driver"

# 磁盘占用全景（镜像/容器/卷/构建缓存，df -v 看明细）
docker system df
docker system df -v

# 查看镜像分层与各层 ID/大小（核对层模型）
docker image history <image>
docker image history --no-trunc <image>

# 查看容器挂载详情（Mounts JSON：卷/bind/tmpfs 与传播模式）
docker inspect <container> --format '{{json .Mounts}}'

# 命名卷创建与挂载（--mount 显式语法）
docker volume create mydata
docker run -d --name web --mount type=volume,src=mydata,dst=/usr/share/nginx/html nginx

# bind mount：自动建源目录 + 只读
docker run -d --mount type=bind,src="$(pwd)/conf",dst=/etc/nginx/conf.d,readonly,bind-create-src nginx

# tmpfs：精确大小与权限（默认 mode 1777）
docker run --rm --mount type=tmpfs,dst=/cache,tmpfs-size=100m,tmpfs-mode=1777 alpine sh -c "mount | grep cache"

# image mount：只读挂载另一镜像内容（需 containerd image store）
docker run -it --mount type=image,src=busybox:latest,dst=/busybox alpine ls /busybox

# 空卷预填充控制：禁止首次挂载自动拷贝容器目录内容
docker run -d --mount type=volume,src=mydata,dst=/data,volume-nocopy nginx

# 卷备份（官方 tar 流程，容器内 /data 打包到宿主机 ./backup.tar）
docker run --rm --volumes-from <容器> -v "$(pwd)":/backup ubuntu tar cvf /backup/backup.tar /data

# 卷还原（解到新容器的卷内，--strip 1 去掉顶层目录）
docker run --rm --volumes-from <新容器> -v "$(pwd)":/backup ubuntu tar xvf /backup/backup.tar --strip 1

# 清理悬空卷 / 悬空镜像 / 构建缓存
docker volume prune
docker image prune
docker builder prune

# 查看 overlay2 挂载四元组（lowerdir / upperdir / merged / work）
mount | grep overlay

# 切换存储驱动 / 启用 containerd image store（改 daemon.json 后重启，先 docker save 备份镜像）
# /etc/docker/daemon.json:
#   {"storage-driver": "overlay2"}
#   {"features": {"containerd-snapshotter": true}}
systemctl restart docker
docker info | grep -E "Storage Driver|Driver Status"   # 验证切换生效
```

---

## 相关笔记

- [[dockerfile-best-practices]] — Dockerfile 指令规范、多阶段构建、镜像优化入门（构建侧，2026-07-29）
- [[docker-buildkit-cache-supplychain-security]] — BuildKit 缓存机制、多阶段高级模式（构建侧，2026-08-11）
- [[docker-image-optimization-security]] — 构建密钥、SBOM/Provenance、Scout 扫描（构建侧，2026-08-19）
- [[docker-advanced-compose-swarm-security]] — Docker 进阶：Compose / Swarm / 安全（编排侧，2026-07-27）
- [[docker-compose-swarm-security-deep-dive]] — Compose 配置治理 / Swarm 运维 / 供应链深潜（编排侧，2026-08-10）

本篇是 docker 主题**存储侧首篇**：镜像分层与落盘（存储驱动/CoW/containerd image store）+ 数据持久化四通道（卷/bind/tmpfs/image mount）。与构建侧三篇（层数/缓存/瘦身策略如何造镜像）互补：镜像造好后如何存、容器数据如何持久化。镜像分层知识与 [[docker-buildkit-cache-supplychain-security]] 的层缓存机制互为表里。
