# 个人知识库使用指南

## 目录结构

```
knowledge-base/
├── assets/          # 图片、附件
├── database/        # 数据库相关笔记
├── linux/           # Linux 运维笔记
├── programming/     # 编程相关
└── .obsidian/       # Obsidian 配置
```

## 使用方法

1. **阅读**：直接在 Obsidian 中打开 vault，或通过 Hermes 读取
2. **创建笔记**：通过 Hermes 的 `write_file` 或 Obsidian 手动创建
3. **同步**：Hermes 的 cronjob 定时 commit + push，Obsidian Git 插件辅助
4. **搜索**：Obsidian 内置搜索，或 Hermes 的 `search_files`

## Obsidian 快捷键

| 功能 | 快捷键 |
|------|--------|
| 新建笔记 | Ctrl+N |
| 搜索 | Ctrl+Shift+F |
| 打开快速切换 | Ctrl+O |
| 链接笔记 | [[ |

## 归档历史

- [[2024-07 知识库初始化]] — 首次搭建
