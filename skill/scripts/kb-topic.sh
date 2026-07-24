#!/bin/bash
# 知识库主题轮转脚本
# 输出格式：星期|优先级|主题名称|子主题|主题目录
# 用法：bash ~/.hermes/scripts/kb-topic.sh

DOW=$(date +%u)  # 1=Mon, 2=Tue, ..., 7=Sun

case $DOW in
  1)
    echo "一 🔴最高 Kubernetes|K8s 核心概念：Pod/Deployment/Service 基础架构与设计原理|k8s"
    ;;
  2)
    echo "二 🔴最高 Docker|Dockerfile 编写规范、多阶段构建、镜像优化与安全|docker"
    ;;
  3)
    echo "三 🟡中 Hadoop生态|HDFS 架构、YARN 资源调度、MapReduce 原理及常用生态工具|hadoop"
    ;;
  4)
    echo "四 🟡中 网络|TCP/IP 基础、DNS 解析原理、负载均衡算法、容器网络 CNI 模型|network"
    ;;
  5)
    echo "五 🟢低 RockyLinux|基础运维命令、systemd 服务管理、firewalld 防火墙、SELinux、dnf 包管理|linux-rocky"
    ;;
  6)
    echo "六 🔴最高 K8s实战|集群排错实战、etcd 备份恢复、监控体系(Prometheus/Grafana)、节点维护|k8s"
    ;;
  7)
    echo "日 🔴最高 Docker进阶|Compose 多服务编排、Swarm 集群模式、镜像安全扫描与运行时安全|docker"
    ;;
esac
