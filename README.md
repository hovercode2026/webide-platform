# WebIDE 云开发平台（基于 minikube + VSCode Server）

在测试机 **192.168.0.158**（Ubuntu 16.04 / 12 核 / 3.8G 内存 / 50G 磁盘）上搭建的多租户 WebIDE 平台。
用户通过网页登录后，一键创建基于 **VSCode Server（code-server）** 的云开发实例，浏览器直接写代码。

## 架构

```
浏览器 ──> WebIDE 平台 (Flask, NodePort 30080, namespace=webide)
              │  k8s API (ServiceAccount + RBAC)
              ├──> 每个实例 = PVC(5Gi) + Deployment(code-server, 1C/1G) + Service(NodePort)
              └──> 用户浏览器 ──直连──> http://192.168.0.158:<NodePort>  (code-server)
```

- **编排层**：minikube v1.32 单节点集群（docker 驱动，Kubernetes v1.28，给集群分配 6C / 2.4G）
- **平台**：Flask + kubernetes python client，跑在集群内 `webide` 命名空间，NodePort **30080**
- **实例**：`ghcr.io/coder/code-server`，资源限制 **1 核 CPU / 1Gi 内存 / 5Gi 磁盘（PVC）**
- **持久化**：minikube hostPath provisioner（`standard` StorageClass），工作区在容器 `/home/coder/project`
- **访问**：实例无独立密码（`--auth=none`，测试环境/内网使用），NodePort 池 30001-30020

## 资源与配额

创建实例时可在弹窗中选择**规格套餐**（大厂机型模式，由平台内置定义，防止用户随意填值）：

| 套餐 | CPU | 内存 | 磁盘 | 适用 |
|---|---|---|---|---|
| 基础型（默认） | 1 核 | 1Gi | 5Gi | 日常开发 |
| 轻量型 | 1 核 | 512Mi | 5Gi | 轻量脚本 / 提高并发数 |
| 增强型 | 2 核 | 2Gi | 10Gi | 大型工程 / 编译 |

| 项目 | 值 |
|---|---|
| 每实例内存 | request 为上限的一半 / **limit = 套餐内存** |
| 每实例磁盘 | PVC = 套餐磁盘 + 临时存储 limit 同值 |
| 实例内存预算 | **2100Mi**（minikube 容器 2400Mi 减去系统开销）；运行中实例的内存上限之和不得超过预算，创建/启动时动态校验 |
| 磁盘配额 | 所有持久卷之和 ≤ **35Gi**（50G 盘扣除镜像/系统占用） |
| 最大实例总数 | **6**（含已停止实例） |

> 并发数不再固定：跑轻量型最多 3-4 个，全跑基础型最多 2 个，增强型一次只能跑 1 个；
> 内存余量不足时弹窗中套餐自动置灰并提示原因，接口侧同样拦截。

## 使用

1. 浏览器打开 `http://192.168.0.158:30080`
2. 登录（账号由 `WEBIDE_USER` / `WEBIDE_PASS` 环境变量或 `webide-platform-auth` Secret 控制；
   两者都未配置时，平台每次重启随机生成初始密码并打印在 Pod 日志中——**仓库内不保存任何密码**）
3. 点击「创建实例」，输入名称并选择规格套餐
4. 状态变为「运行中」后，点「打开 IDE」即可在平台内嵌窗口或新窗口中使用 VSCode
5. 不用时「停止」释放 CPU/内存（数据保留在 PVC），「删除」则连同数据一起清除

## 日常运维（SSH 到 192.168.0.158 后）

```bash
# 查看平台与实例
kubectl -n webide get pods,svc,pvc

# 集群开关机（minikube 停止后实例与数据保留）
minikube stop
minikube start --driver=docker --cpus=6 --memory=2400mb --disk-size=30g \
  --kubernetes-version=v1.28.3 --image-mirror-country=cn --force \
  --ports=30080:30080 --ports=30001-30020:30001-30020
# 注意 --ports 必须带上，否则 NodePort(30080/30001-30020) 无法从外部访问

# 修改平台代码/配置后发布（必须走此脚本：脚本会 apply 配置并 set image；
# 单独 kubectl apply 会把镜像 tag 回滚成 yaml 里的旧值）
bash /home/admin/webide-platform/build-deploy.sh

# 查看平台日志
kubectl -n webide logs -f deploy/webide-platform
```

> 注意：minikube 集群随虚拟机重启后需要 `minikube start` 拉起（数据不丢）。
> 若执行了 `minikube delete`，PVC 数据会全部丢失，请勿随意删除集群。

## 本目录文件

- `webide-platform/app/main.py` — 平台后端（Flask，实例编排逻辑）
- `webide-platform/app/static/index.html` — 平台前端（中文单页应用）
- `webide-platform/app/requirements.txt` — python 依赖
- `webide-platform/Dockerfile` — 平台镜像构建（基础镜像走 daocloud，pip 走阿里云）
- `webide-platform/deploy/platform.yaml` — Namespace/ServiceAccount/RBAC/Deployment/Service
- `webide-platform/build-deploy.sh` — 构建镜像 + 注入 minikube + 部署一条龙
- `tools/ssh_run.py` / `tools/ssh_put.py` — 本地辅助脚本（paramiko SSH/SFTP）

## 环境要点（本机特有）

- Ubuntu 16.04 内核 4.4 较老，docker-ce 装的是 xenial 最后支持的 **20.10.7**（阿里云源）
- minikube 的 CN 镜像映射会用阿里云 OSS 下载 k8s 二进制，但该源缺 v1.28.3，
  已通过把 kubeadm/kubelet/kubectl（阿里云 kubernetes-new 仓库 v1.28.15）预填到
  `~/.minikube/cache/linux/amd64/v1.28.3/` 解决；**如重装 minikube 需保留该缓存**
- code-server 镜像来自 ghcr.io（可直连）；docker hub 类镜像走 docker.m.daocloud.io
- 磁盘 5Gi 为 PVC 声明值 + 临时存储 limit 硬限；hostPath 类 PVC 精确配额依赖底层文件系统，
  测试环境以「临时存储 limit 5Gi」作为硬性兜底

## 后续扩展方向

- 平台加 HTTPS 与独立用户体系（每个用户可见自己的实例，名称加用户前缀）
- 接入 metrics-server 展示实例实时 CPU/内存曲线
- 对接 Git（内置凭据、仓库模板）、Prebuild 镜像（带常用插件的 code-server 自定义镜像）
- 多节点：将 minikube 换成正式 k8s 集群后平台逻辑无需改动（部署目标命名空间即可）
