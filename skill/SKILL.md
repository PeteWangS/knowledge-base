---
name: knowledge-base
description: 个人知识库管理 — Git + Obsidian 双驱动，GitHub 夜间自动研究 → 晨间归档为结构化笔记
platforms: [linux, macos, windows]
---

# Personal Knowledge Base (Auto-Research System)

Git + Obsidian 个人知识库，含夜间自动研究流水线。

## 前置条件

1. Obsidian vault 已初始化 git 并关联远程仓库
2. `OBSIDIAN_VAULT_PATH` 环境变量已设置
3. 主题轮转脚本 `~/.hermes/scripts/kb-topic.sh`
4. 渲染脚本 `~/.hermes/scripts/render-mermaid.py`

## 环境变量

```bash
OBSIDIAN_VAULT_PATH=/mnt/d/software/obsidianDocument
```

## 目录规范

```
{OBSIDIAN_VAULT_PATH}/
├── k8s/ docker/ hadoop/ network/ linux-rocky/   # 笔记按主题分类
├── assets/{topic}/                                # SVG 图片
├── skill/                                        # 本文档
└── .staging/                                     # 临时文件（自动管理）
```

## 操作指南

### 文件 CRUD（推荐用 Hermes 工具）

```markdown
读:   read_file(path="$OBSIDIAN_VAULT_PATH/k8s/xxx.md")
写:   write_file(path="$OBSIDIAN_VAULT_PATH/xxx.md", content="...")
搜索: search_files(pattern="关键词", path="$OBSIDIAN_VAULT_PATH")
```

### Git 操作

```bash
cd $OBSIDIAN_VAULT_PATH && git add -A && git commit -m "msg" && git push
cd $OBSIDIAN_VAULT_PATH && git pull --rebase
cd $OBSIDIAN_VAULT_PATH && git log --oneline -10
```

### 图片渲染

```bash
# 只设子图背景（有自定义颜色时）
python3 ~/.hermes/scripts/render-mermaid.py input.mmd assets/k8s/output.svg

# 完整主题渲染（无自定义颜色时）
python3 ~/.hermes/scripts/render-mermaid.py input.mmd assets/k8s/output.svg

# 指定主题
python3 ~/.hermes/scripts/render-mermaid.py input.mmd output.svg --theme docker
```

主题自动根据 `assets/{topic}/` 路径匹配：k8s(深海蓝)、docker(碧海青)、hadoop(琥珀暖)、network(翠林绿)、linux-rocky(苍岭灰)。

---

## 自动流水线

### 两个 Cron Job

```yaml
kb-nightly-research:
  时间: 每天 20:00
  动作: 研究当日主题 → .staging/research.json

kb-morning-compile:
  时间: 每天 08:10
  动作: 读取研究 → 生成 Mermaid 图 → 渲染 SVG → 写笔记 → git commit+push
```

### 主题轮转

| 星期 | 主题 | 优先级 |
|------|------|--------|
| 一 | K8s 基础 | 🔴最高 |
| 二 | Docker | 🔴最高 |
| 三 | Hadoop 生态 | 🟡中 |
| 四 | 网络 | 🟡中 |
| 五 | Rocky Linux | 🟢低 |
| 六 | K8s 实战 | 🔴最高 |
| 日 | Docker 进阶 | 🔴最高 |

### 管理命令

```bash
cronjob action=list                               # 查看状态
cronjob action=run job_id=ba41b0c5a996            # 手动研究
cronjob action=run job_id=e4bb9f117f45            # 手动归档
bash ~/.hermes/scripts/kb-topic.sh                # 查看今日主题
```

---

## 故障排查

- 图片渲染失败：mermaid.ink API 可能限流，等几分钟重试
- git push 失败：检查 SSH key、网络连接
- 笔记丢失：`git log` 查看历史，`git checkout` 恢复
