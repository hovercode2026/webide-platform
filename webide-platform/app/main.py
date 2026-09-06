"""WebIDE 平台后端 — 基于 minikube/Kubernetes 编排 VSCode Server (code-server) 实例。

每个实例包含:
  - PVC  webide-ws-<name>      规格套餐决定 (5Gi/10Gi, 工作区/家目录持久化)
  - Deployment webide-<name>   code-server, 资源限制由规格套餐决定
  - Service webide-<name>      NodePort (30001-30020 池), 对外提供浏览器访问

实例规格套餐(flavor): 用户创建时选择, 资源校验按集群内存/磁盘预算动态进行。
"""
import os
import re
import secrets
import time

from flask import Flask, jsonify, request, session, send_from_directory

from kubernetes import client, config
from kubernetes.client.rest import ApiException

APP_NS = os.environ.get("WEBIDE_NS", "webide")
APP_USER = os.environ.get("WEBIDE_USER", "admin")
# 生产/公开环境请务必通过环境变量 WEBIDE_PASS 设置密码；
# 未设置时每次启动随机生成（打印在平台 Pod 日志中），仓库内不保存任何密码
APP_PASS = os.environ.get("WEBIDE_PASS") or secrets.token_hex(4)
CODE_IMAGE = os.environ.get("WEBIDE_CODE_IMAGE", "ghcr.io/coder/code-server:latest")
MAX_TOTAL = int(os.environ.get("WEBIDE_MAX_TOTAL", "6"))
PORT_POOL_START = int(os.environ.get("WEBIDE_PORT_POOL_START", "30001"))
PORT_POOL_END = int(os.environ.get("WEBIDE_PORT_POOL_END", "30020"))
# minikube 节点容器 2400Mi, 预留系统 Pod 与平台自身开销后, 实例可用内存预算
MEM_BUDGET_MI = int(os.environ.get("WEBIDE_MEM_BUDGET_MI", "2100"))
DISK_BUDGET_GI = int(os.environ.get("WEBIDE_DISK_BUDGET_GI", "35"))

# 规格套餐: cpu 核数 / memMi 内存上限 / diskGi 磁盘
FLAVORS = {
    "b": {"name": "基础型", "desc": "日常开发", "cpu": "1", "memMi": 1024, "diskGi": 5},
    "l": {"name": "轻量型", "desc": "轻量脚本/高并发", "cpu": "1", "memMi": 512, "diskGi": 5},
    "s": {"name": "增强型", "desc": "大型工程/编译", "cpu": "2", "memMi": 2048, "diskGi": 10},
}
DEFAULT_FLAVOR = "b"

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("WEBIDE_SECRET") or secrets.token_hex(32)

config.load_incluster_config()
core = client.CoreV1Api()
apps = client.AppsV1Api()

if os.environ.get("WEBIDE_PASS"):
    print("[webide] login password loaded from WEBIDE_PASS env", flush=True)
else:
    print("[webide] WEBIDE_PASS not set, generated random initial password: %s (user: %s)"
          % (APP_PASS, APP_USER), flush=True)


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def login_required(fn):
    def wrapper(*a, **kw):
        if not session.get("user"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        return fn(*a, **kw)

    wrapper.__name__ = fn.__name__
    return wrapper


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})


@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    if body.get("username") == APP_USER and body.get("password") == APP_PASS:
        session["user"] = APP_USER
        session.permanent = True
        return jsonify({"ok": True})
    return err("用户名或密码错误", 401)


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/session")
def get_session():
    return jsonify({"ok": True, "user": session.get("user")})


# ------------------------------------------------------------- helpers

def inst_labels(name):
    return {"app": "webide-instance", "instance": name, "app.kubernetes.io/part-of": "webide"}


def alloc_nodeport():
    used = set()
    for s in core.list_namespaced_service(APP_NS).items:
        for p in (s.spec.ports or []):
            if p.node_port:
                used.add(p.node_port)
    for np in range(PORT_POOL_START, PORT_POOL_END + 1):
        if np not in used:
            return np
    return None


def list_deploys():
    return apps.list_namespaced_deployment(APP_NS, label_selector="app=webide-instance").items


def deploy_mem_mi(d):
    """Deployment 的内存上限 (Mi)"""
    try:
        mem = d.spec.template.spec.containers[0].resources.limits.get("memory", "0")
    except (AttributeError, IndexError, TypeError):
        return 0
    mem = mem.strip()
    if mem.endswith("Gi"):
        return int(float(mem[:-2]) * 1024)
    if mem.endswith("Mi"):
        return int(float(mem[:-2]))
    if mem.endswith("Ki"):
        return int(float(mem[:-2]) / 1024)
    return 0


def deploy_flavor_id(d):
    ann = (d.metadata.annotations or {}).get("webide.io/flavor")
    if ann in FLAVORS:
        return ann
    mem = deploy_mem_mi(d)
    for fid, f in FLAVORS.items():
        if f["memMi"] == mem:
            return fid
    return "b"


def running_mem_mi(deploys=None):
    deploys = deploys if deploys is not None else list_deploys()
    return sum(deploy_mem_mi(d) for d in deploys if (d.spec.replicas or 0) > 0)


def used_disk_gi():
    total = 0
    for pvc in core.list_namespaced_persistent_volume_claim(
            APP_NS, label_selector="app=webide-instance").items:
        s = pvc.spec.resources.requests.get("storage", "0Gi").strip()
        if s.endswith("Gi"):
            total += int(float(s[:-2]))
        elif s.endswith("Mi"):
            total += int(float(s[:-2])) / 1024
    return total


def mem_str(mi):
    return "%dGi" % (mi // 1024) if mi >= 1024 else "%dMi" % mi


def pod_state(name):
    try:
        pods = core.list_namespaced_pod(APP_NS, label_selector="app=webide-instance,instance=" + name).items
    except ApiException:
        return "启动中"
    if not pods:
        return "启动中"
    p = pods[0]
    phase = p.status.phase
    if phase == "Running":
        cs = (p.status.container_statuses or [None])[0]
        if cs and cs.ready:
            return "运行中"
        waiting = cs and cs.state and cs.state.waiting
        if waiting and waiting.reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError"):
            return "异常: " + waiting.reason
        return "启动中"
    if phase == "Pending":
        return "调度中"
    if phase in ("Failed", "Unknown"):
        return "异常"
    return phase


# ------------------------------------------------------------- flavors

@app.route("/api/flavors")
@login_required
def list_flavors():
    used = running_mem_mi()
    items = []
    for fid, f in FLAVORS.items():
        items.append({
            "id": fid, "name": f["name"], "desc": f["desc"],
            "cpu": f["cpu"], "mem": mem_str(f["memMi"]), "disk": "%dGi" % f["diskGi"],
            "fit": used + f["memMi"] <= MEM_BUDGET_MI,
        })
    return jsonify({"ok": True, "flavors": items,
                    "memUsedMi": used, "memBudgetMi": MEM_BUDGET_MI, "diskUsedGi": used_disk_gi(),
                    "diskBudgetGi": DISK_BUDGET_GI})


# ------------------------------------------------------------- instances

@app.route("/api/instances")
@login_required
def list_instances():
    deploys = sorted(list_deploys(), key=lambda x: x.metadata.creation_timestamp or 0)
    disks = {p.metadata.labels.get("instance", ""): p.spec.resources.requests.get("storage", "")
             for p in core.list_namespaced_persistent_volume_claim(
                 APP_NS, label_selector="app=webide-instance").items}
    items = []
    for d in deploys:
        name = d.metadata.labels.get("instance", "")
        fid = deploy_flavor_id(d)
        f = FLAVORS[fid]
        np = None
        try:
            svc = core.read_namespaced_service("webide-" + name, APP_NS)
            if svc.spec.type == "NodePort":
                np = svc.spec.ports[0].node_port
        except ApiException:
            pass
        state = "已停止" if (d.spec.replicas or 0) == 0 else pod_state(name)
        items.append({
            "name": name, "state": state, "nodePort": np,
            "flavor": fid, "flavorName": f["name"],
            "resources": {"cpu": f["cpu"], "mem": mem_str(f["memMi"]),
                          "disk": disks.get(name, "%dGi" % f["diskGi"])},
            "createdAt": (d.metadata.creation_timestamp.strftime("%Y-%m-%d %H:%M") if d.metadata.creation_timestamp else "-"),
        })
    return jsonify({"ok": True, "instances": items, "runningMemMi": running_mem_mi(deploys),
                    "memBudgetMi": MEM_BUDGET_MI, "maxTotal": MAX_TOTAL,
                    "diskUsedGi": used_disk_gi(), "diskBudgetGi": DISK_BUDGET_GI})


@app.route("/api/instances", methods=["POST"])
@login_required
def create_instance():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    fid = body.get("flavor") or DEFAULT_FLAVOR
    if fid not in FLAVORS:
        return err("无效的规格套餐")
    f = FLAVORS[fid]
    if not NAME_RE.match(name):
        return err("实例名仅支持小写字母/数字/中划线，以字母或数字开头结尾，长度2-20")
    deploys = list_deploys()
    if any(d.metadata.name == "webide-" + name for d in deploys):
        return err("实例已存在")
    if len(deploys) >= MAX_TOTAL:
        return err("实例总数已达上限 %d 个" % MAX_TOTAL, 409)
    used_mem = running_mem_mi(deploys)
    if used_mem + f["memMi"] > MEM_BUDGET_MI:
        return err("集群内存余量不足：需 %dMi，可用 %dMi（已用 %dMi/预算 %dMi）。请先停止部分实例或选择更低规格。"
                   % (f["memMi"], MEM_BUDGET_MI - used_mem, used_mem, MEM_BUDGET_MI), 409)
    used_disk = used_disk_gi()
    if used_disk + f["diskGi"] > DISK_BUDGET_GI:
        return err("磁盘配额不足：现有 %dGi + 新增 %dGi 超过上限 %dGi，请删除不需要的实例。"
                   % (used_disk, f["diskGi"], DISK_BUDGET_GI), 409)
    np = alloc_nodeport()
    if np is None:
        return err("NodePort 资源已用尽", 409)

    labels = inst_labels(name)
    mem_req = max(256, f["memMi"] // 2)
    cpu_req = "%dm" % (int(f["cpu"]) * 100)
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name="webide-ws-" + name, labels=labels),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="standard",
            resources=client.V1ResourceRequirements(requests={"storage": "%dGi" % f["diskGi"]}),
        ),
    )
    deploy = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name="webide-" + name, labels=labels,
            annotations={"webide.io/flavor": fid}),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": "webide-instance", "instance": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    init_containers=[client.V1Container(
                        name="init-home",
                        image=CODE_IMAGE,
                        command=["sh", "-c", "mkdir -p /home/coder/project && chown -R 1000:1000 /home/coder || true"],
                        volume_mounts=[client.V1VolumeMount(name="home", mount_path="/home/coder")],
                        security_context=client.V1SecurityContext(run_as_user=0),
                    )],
                    containers=[client.V1Container(
                        name="code-server",
                        image=CODE_IMAGE,
                        image_pull_policy="IfNotPresent",
                        args=["--auth=none", "--bind-addr=0.0.0.0:8080", "/home/coder/project"],
                        ports=[client.V1ContainerPort(container_port=8080)],
                        resources=client.V1ResourceRequirements(
                            requests={"cpu": cpu_req, "memory": mem_str(mem_req)},
                            limits={"cpu": f["cpu"], "memory": mem_str(f["memMi"]),
                                    "ephemeral-storage": "%dGi" % f["diskGi"]},
                        ),
                        readiness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(path="/healthz", port=8080),
                            initial_delay_seconds=5, period_seconds=5, failure_threshold=12,
                        ),
                        volume_mounts=[client.V1VolumeMount(name="home", mount_path="/home/coder")],
                    )],
                    volumes=[client.V1Volume(
                        name="home",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="webide-ws-" + name),
                    )],
                ),
            ),
        ),
    )
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name="webide-" + name, labels=labels),
        spec=client.V1ServiceSpec(
            type="NodePort",
            selector={"app": "webide-instance", "instance": name},
            ports=[client.V1ServicePort(port=8080, target_port=8080, node_port=np)],
        ),
    )
    try:
        core.create_namespaced_persistent_volume_claim(APP_NS, pvc)
        apps.create_namespaced_deployment(APP_NS, deploy)
        core.create_namespaced_service(APP_NS, svc)
    except ApiException as e:
        return err("创建失败: %s" % e.reason, 500)
    return jsonify({"ok": True, "name": name, "flavor": fid, "nodePort": np})


@app.route("/api/instances/<name>", methods=["DELETE"])
@login_required
def delete_instance(name):
    try:
        apps.delete_namespaced_deployment("webide-" + name, APP_NS)
        core.delete_namespaced_service("webide-" + name, APP_NS)
        core.delete_namespaced_persistent_volume_claim("webide-ws-" + name, APP_NS)
    except ApiException as e:
        if e.status != 404:
            return err("删除失败: %s" % e.reason, 500)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/stop", methods=["POST"])
@login_required
def stop_instance(name):
    try:
        apps.patch_namespaced_deployment_scale("webide-" + name, APP_NS, {"spec": {"replicas": 0}})
    except ApiException as e:
        return err("停止失败: %s" % e.reason, 500)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/start", methods=["POST"])
@login_required
def start_instance(name):
    try:
        d = apps.read_namespaced_deployment("webide-" + name, APP_NS)
    except ApiException as e:
        return err("实例不存在: %s" % e.reason, 404)
    need = deploy_mem_mi(d)
    used = running_mem_mi()
    if used + need > MEM_BUDGET_MI:
        return err("集群内存余量不足：启动需 %dMi，可用 %dMi。请先停止其他实例。"
                   % (need, MEM_BUDGET_MI - used), 409)
    try:
        apps.patch_namespaced_deployment_scale("webide-" + name, APP_NS, {"spec": {"replicas": 1}})
    except ApiException as e:
        return err("启动失败: %s" % e.reason, 500)
    return jsonify({"ok": True})


@app.route("/api/capacity")
@login_required
def capacity():
    nodes = core.list_node().items
    if not nodes:
        return err("无节点", 500)
    n = nodes[0]
    alloc = n.status.allocatable or {}
    cap = n.status.capacity or {}
    deploys = list_deploys()
    return jsonify({
        "ok": True,
        "node": n.metadata.name,
        "cpuCores": cap.get("cpu", "?"),
        "cpuAllocatable": alloc.get("cpu", "?"),
        "memoryAllocatable": alloc.get("memory", "?"),
        "podCapacity": cap.get("pods", "?"),
        "instancesTotal": len(deploys),
        "maxTotal": MAX_TOTAL,
        "runningMemMi": running_mem_mi(deploys),
        "memBudgetMi": MEM_BUDGET_MI,
        "diskUsedGi": used_disk_gi(),
        "diskBudgetGi": DISK_BUDGET_GI,
        "version": {"kubeletVersion": (n.status.node_info.kubelet_version if n.status.node_info else "?")},
    })


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
