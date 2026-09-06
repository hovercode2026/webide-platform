#!/bin/bash
# 构建 WebIDE 平台镜像并部署到 minikube（时间戳 tag，确保每次更新都生效）
set -e
cd /home/admin/webide-platform
TAG=build-$(date +%Y%m%d%H%M%S)

echo "==> 构建平台镜像 webide-platform:$TAG"
docker build -t webide-platform:$TAG . 2>&1 | tail -2

echo "==> 注入镜像到 minikube 节点"
minikube image load webide-platform:$TAG

echo "==> 更新 Deployment 镜像并等待就绪 (namespace=webide, NodePort=30080)"
kubectl -n webide set image deploy/webide-platform platform=webide-platform:$TAG
kubectl -n webide rollout status deploy/webide-platform --timeout=240s

echo "==> 清理节点内旧镜像"
OLD_TAGS=$(minikube image ls 2>/dev/null | grep '^webide-platform' | grep -v "$TAG" | awk '{print $1":"$2}' | head -5)
if [ -n "$OLD_TAGS" ]; then minikube ssh -- "sudo crictl rmi $OLD_TAGS 2>/dev/null; docker rmi $OLD_TAGS 2>/dev/null" >/dev/null 2>&1 || true; fi

echo "==> 平台状态"
kubectl -n webide get pods,svc -l app=webide-platform
