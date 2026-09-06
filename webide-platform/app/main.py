"""WebIDE 平台后端 — 基于 minikube/Kubernetes 编排 VSCode Server (code-server) 实例。

能力总览:
  - 多用户: 注册/登录/改密 (SQLite), 实例按用户隔离, 内置管理员, 用户管理
  - 实例:   规格套餐 + 初始化模板, PVC 持久化, NodePort 直连, 启停/重启/删除
  - 端口:   应用端口预览(每实例最多 2 个), NodePort 自动分配
  - 智能休眠: 空闲实例自动停止(可配置开关/时长), 释放内存预算
  - 监控:   metrics-server 采样(总量+实例级), 历史曲线
  - 终端:   实例内命令执行(带超时)
  - 审计:   全量操作流水
"""
import hashlib
import json as _json
import os
import re
import secrets
import shlex
import sqlite3
import threading
import time
from collections import deque

from flask import Flask, jsonify, request, session, send_from_directory

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_exec_stream

APP_NS = os.environ.get("WEBIDE_NS", "webide")
CODE_IMAGE = os.environ.get("WEBIDE_CODE_IMAGE", "ghcr.io/coder/code-server:latest")
DB_PATH = os.environ.get("WEBIDE_DB", "/data/webide.db")
MAX_TOTAL = int(os.environ.get("WEBIDE_MAX_TOTAL", "6"))
PORT_POOL_START = int(os.environ.get("WEBIDE_PORT_POOL_START", "30001"))
PORT_POOL_END = int(os.environ.get("WEBIDE_PORT_POOL_END", "30020"))
MEM_BUDGET_MI = int(os.environ.get("WEBIDE_MEM_BUDGET_MI", "2100"))
DISK_BUDGET_GI = int(os.environ.get("WEBIDE_DISK_BUDGET_GI", "35"))
USER_MAX_RUNNING = int(os.environ.get("WEBIDE_USER_MAX_RUNNING", "2"))
MAX_EXTRA_PORTS = int(os.environ.get("WEBIDE_MAX_EXTRA_PORTS", "2"))

FLAVORS = {
    "b": {"name": "基础型", "desc": "日常开发", "cpu": "1", "memMi": 1024, "diskGi": 5},
    "l": {"name": "轻量型", "desc": "轻量脚本/高并发", "cpu": "1", "memMi": 512, "diskGi": 5},
    "s": {"name": "增强型", "desc": "大型工程/编译", "cpu": "2", "memMi": 2048, "diskGi": 10},
}
DEFAULT_FLAVOR = "b"

_TPL_PY = r'''if [ -z "$(ls -A /home/coder/project 2>/dev/null)" ]; then
  cat > /home/coder/project/README.md <<'EOF'
# Python 示例工程

python main.py 运行, 或安装依赖: pip install -r requirements.txt
EOF
  cat > /home/coder/project/main.py <<'EOF'
print("Hello from WebIDE on Kubernetes!")


def add(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    print("2 + 3 =", add(2, 3))
EOF
  cat > /home/coder/project/requirements.txt <<'EOF'
# 在此添加依赖
EOF
fi'''

_TPL_NODE = r'''if [ -z "$(ls -A /home/coder/project 2>/dev/null)" ]; then
  cat > /home/coder/project/README.md <<'EOF'
# Node.js 示例工程

npm install 后 node app.js 运行
EOF
  cat > /home/coder/project/app.js <<'EOF'
console.log("Hello from WebIDE on Kubernetes!");

function add(a, b) {
  return a + b;
}

console.log("2 + 3 =", add(2, 3));
EOF
  cat > /home/coder/project/package.json <<'EOF'
{
  "name": "webide-demo",
  "version": "1.0.0",
  "main": "app.js",
  "scripts": { "start": "node app.js" }
}
EOF
fi'''

_TPL_BLANK = r'''mkdir -p /home/coder/project'''

TEMPLATES = {
    "blank": {"name": "空白工程", "script": _TPL_BLANK},
    "python": {"name": "Python", "script": _TPL_PY},
    "node": {"name": "Node.js", "script": _TPL_NODE},
}

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


app = Flask(__name__, static_folder="static", static_url_path="/static")
config.load_incluster_config()
core = client.CoreV1Api()
apps = client.AppsV1Api()
custom = client.CustomObjectsApi()

# ------------------------------------------------------------------ 存储

_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def hash_pw(salt, pwd):
    return hashlib.sha256((salt + pwd).encode()).hexdigest()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = db()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, salt TEXT NOT NULL, passhash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user', created_at REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        user TEXT, action TEXT, target TEXT, detail TEXT);
      CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
    """)
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        pw = os.environ.get("WEBIDE_PASS") or secrets.token_hex(4)
        salt = secrets.token_hex(8)
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?)",
                     ("admin", salt, hash_pw(salt, pw), "admin", time.time()))
        if os.environ.get("WEBIDE_PASS"):
            print("[webide] 管理员账号 admin, 密码来自 WEBIDE_PASS 环境变量", flush=True)
        else:
            print("[webide] 未设置 WEBIDE_PASS, 已生成随机管理员密码: %s" % pw, flush=True)
    row = conn.execute("SELECT v FROM settings WHERE k='secret'").fetchone()
    if not row:
        conn.execute("INSERT INTO settings VALUES('secret',?)", (secrets.token_hex(32),))
    conn.commit()
    conn.close()


init_db()
_conn = db()
app.secret_key = _conn.execute("SELECT v FROM settings WHERE k='secret'").fetchone()["v"]
_conn.close()


def get_setting(k, default=""):
    conn = db()
    row = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    conn.close()
    return row["v"] if row else default


def set_setting(k, v):
    with _lock:
        conn = db()
        conn.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        conn.commit()
        conn.close()


def audit(user, action, target="", detail=""):
    try:
        with _lock:
            conn = db()
            conn.execute("INSERT INTO audit(ts,user,action,target,detail) VALUES(?,?,?,?,?)",
                         (time.time(), user, action, target, detail))
            conn.commit()
            conn.close()
    except Exception as e:
        print("[webide] audit failed:", e, flush=True)


def current_user():
    name = session.get("user")
    if not name:
        return None
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def login_required(fn):
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"ok": False, "error": "未登录"}), 401
        return fn(u, *a, **kw)

    wrapper.__name__ = fn.__name__
    return wrapper


def admin_required(fn):
    def wrapper(u, *a, **kw):
        if u["role"] != "admin":
            return jsonify({"ok": False, "error": "需要管理员权限"}), 403
        return fn(u, *a, **kw)

    wrapper.__name__ = fn.__name__
    return wrapper


# ------------------------------------------------------------------ helpers

def inst_labels(name, owner):
    return {"app": "webide-instance", "instance": name, "webide.io/owner": owner,
            "app.kubernetes.io/part-of": "webide"}


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


def list_deploys(owner=None):
    sel = "app=webide-instance,webide.io/owner=" + owner if owner else "app=webide-instance"
    return apps.list_namespaced_deployment(APP_NS, label_selector=sel).items


def deploy_owner(d):
    return d.metadata.labels.get("webide.io/owner", "admin")


def deploy_mem_mi(d):
    try:
        mem = d.spec.template.spec.containers[0].resources.limits.get("memory", "0").strip()
    except (AttributeError, IndexError, TypeError):
        return 0
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


def user_running_count(owner, deploys=None):
    deploys = deploys if deploys is not None else list_deploys()
    return sum(1 for d in deploys if deploy_owner(d) == owner and (d.spec.replicas or 0) > 0)


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


def svc_extra_ports(svc):
    return [{"port": p.port, "nodePort": p.node_port}
            for p in (svc.spec.ports or []) if p.port != 8080]


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
        if waiting and waiting.reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
            return "异常: " + waiting.reason
        return "启动中"
    if phase == "Pending":
        return "调度中"
    if phase in ("Failed", "Unknown"):
        return "异常"
    return phase


def get_deploy_checked(name, user):
    try:
        d = apps.read_namespaced_deployment("webide-" + name, APP_NS)
    except ApiException as e:
        return None, ("实例不存在: %s" % e.reason, 404)
    if user["role"] != "admin" and deploy_owner(d) != user["username"]:
        return None, ("无权操作他人实例", 403)
    return d, None


# ------------------------------------------------------------------ 监控采样

def _parse_cpu_m(v):
    v = str(v).strip()
    if v.endswith("n"):
        return float(v[:-1]) / 1e6
    if v.endswith("u"):
        return float(v[:-1]) / 1e3
    if v.endswith("m"):
        return float(v[:-1])
    return float(v) * 1000


def _parse_mem_mi(v):
    v = str(v).strip()
    if v.endswith("Gi"):
        return float(v[:-2]) * 1024
    if v.endswith("Mi"):
        return float(v[:-2])
    if v.endswith("Ki"):
        return float(v[:-2]) / 1024
    return float(v) / 1048576


def fetch_pod_metrics():
    try:
        data = custom.list_namespaced_custom_object(
            "metrics.k8s.io", "v1beta1", APP_NS, "pods")
    except Exception:
        return {}
    result = {}
    for pod in data.get("items", []):
        name = pod.get("metadata", {}).get("labels", {}).get("instance")
        if not name:
            continue
        acc = result.setdefault(name, {"cpu_m": 0.0, "mem_mi": 0.0})
        for c in pod.get("containers", []):
            usage = c.get("usage", {}) or {}
            try:
                acc["cpu_m"] = round(acc["cpu_m"] + _parse_cpu_m(usage.get("cpu", "0")), 1)
                acc["mem_mi"] = round(acc["mem_mi"] + _parse_mem_mi(usage.get("memory", "0Ki")), 1)
            except ValueError:
                pass
    return result


history = deque(maxlen=240)
host_hist = deque(maxlen=240)
host_latest = {}
idle_since = {}
CPU_IDLE_M = 5.0


def _cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(float, parts[:8]))
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def host_snapshot(prev_cpu):
    """容器与宿主机共享内核, /proc 与根文件系统直接反映宿主机真实状态。"""
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            mem[k.strip()] = float(v.split()[0])  # kB
    total_mi = mem["MemTotal"] / 1024
    used_mi = total_mi - mem.get("MemAvailable", mem.get("MemFree", 0)) / 1024
    st = os.statvfs("/")
    disk_total_gi = st.f_blocks * st.f_frsize / 2**30
    disk_used_gi = disk_total_gi - st.f_bavail * st.f_frsize / 2**30
    prev_idle, prev_total = prev_cpu
    cur_idle, cur_total = _cpu_times()
    cpu_pct = 0.0
    if cur_total > prev_total:
        cpu_pct = round(max(0.0, min(100.0, (1 - (cur_idle - prev_idle) / (cur_total - prev_total)) * 100)), 1)
    return ({"mem_total_mi": round(total_mi), "mem_used_mi": round(used_mi),
             "disk_total_gi": round(disk_total_gi, 1), "disk_used_gi": round(disk_used_gi, 1),
             "cpu_pct": cpu_pct, "cores": os.cpu_count() or 1,
             "load1": float(open("/proc/loadavg").read().split()[0]),
             "ts": int(time.time())},
            (cur_idle, cur_total))


def idle_check(m, now):
    """空闲实例自动休眠: 连续 idle_minutes 分钟 CPU 低于阈值则停止, 释放内存预算。"""
    if get_setting("idle_enabled", "1") != "1":
        idle_since.clear()
        return
    try:
        threshold_min = int(get_setting("idle_minutes", "30"))
    except ValueError:
        threshold_min = 30
    deploys = list_deploys()
    for d in deploys:
        if (d.spec.replicas or 0) == 0:
            idle_since.pop(d.metadata.labels.get("instance", ""), None)
            continue
        name = d.metadata.labels.get("instance", "")
        created = d.metadata.creation_timestamp.timestamp() if d.metadata.creation_timestamp else now
        if now - created < 600:  # 新实例 10 分钟保护期
            continue
        cpu = m.get(name, {}).get("cpu_m", 0)
        if cpu < CPU_IDLE_M:
            first = idle_since.setdefault(name, now)
            if now - first >= threshold_min * 60:
                try:
                    apps.patch_namespaced_deployment_scale(
                        "webide-" + name, APP_NS, {"spec": {"replicas": 0}})
                    audit("system", "空闲休眠", name,
                          "CPU 连续 %d 分钟低于 %.0fm, 已自动停止(数据保留)" % (threshold_min, CPU_IDLE_M))
                except Exception as e:
                    print("[webide] idle stop failed:", e, flush=True)
                idle_since.pop(name, None)
        else:
            idle_since.pop(name, None)


def sampler():
    prev_cpu = _cpu_times()
    while True:
        try:
            m = fetch_pod_metrics()
            now = time.time()
            snap = {"ts": int(now),
                    "cpu_m": round(sum(v["cpu_m"] for v in m.values()), 1),
                    "mem_mi": round(sum(v["mem_mi"] for v in m.values()), 1),
                    "pods": {k: dict(v) for k, v in m.items()}}
            host, prev_cpu = host_snapshot(prev_cpu)
            with _lock:
                history.append(snap)
                host_hist.append(host)
                host_latest.clear()
                host_latest.update(host)
            idle_check(m, now)
        except Exception as e:
            print("[webide] sampler:", e, flush=True)
        time.sleep(10)


threading.Thread(target=sampler, daemon=True).start()

# ------------------------------------------------------------------ 认证

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})


@app.route("/api/session")
def get_session():
    u = current_user()
    return jsonify({"ok": True, "user": u["username"] if u else None,
                    "role": u["role"] if u else None})


@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    name, pwd = (body.get("username") or "").strip(), body.get("password") or ""
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    conn.close()
    if row and hash_pw(row["salt"], pwd) == row["passhash"]:
        session["user"] = name
        session.permanent = True
        audit(name, "登录")
        return jsonify({"ok": True, "role": row["role"]})
    return err("用户名或密码错误", 401)


@app.route("/api/register", methods=["POST"])
def register():
    body = request.get_json(silent=True) or {}
    name = (body.get("username") or "").strip()
    pwd = body.get("password") or ""
    if not NAME_RE.match(name):
        return err("用户名仅支持小写字母/数字/中划线，2-20 位")
    if len(pwd) < 6:
        return err("密码至少 6 位")
    conn = db()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
        conn.close()
        return err("用户名已存在")
    salt = secrets.token_hex(8)
    conn.execute("INSERT INTO users VALUES(?,?,?,?,?)",
                 (name, salt, hash_pw(salt, pwd), "user", time.time()))
    conn.commit()
    conn.close()
    session["user"] = name
    session.permanent = True
    audit(name, "注册")
    return jsonify({"ok": True, "role": "user"})


@app.route("/api/password", methods=["POST"])
@login_required
def change_password(u):
    body = request.get_json(silent=True) or {}
    old, new = body.get("old") or "", body.get("new") or ""
    if len(new) < 6:
        return err("新密码至少 6 位")
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (u["username"],)).fetchone()
    if not row or hash_pw(row["salt"], old) != row["passhash"]:
        conn.close()
        return err("原密码错误")
    salt = secrets.token_hex(8)
    conn.execute("UPDATE users SET salt=?, passhash=? WHERE username=?",
                 (salt, hash_pw(salt, new), u["username"]))
    conn.commit()
    conn.close()
    audit(u["username"], "修改密码")
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 规格与模板

@app.route("/api/flavors")
@login_required
def list_flavors(u):
    used = running_mem_mi()
    return jsonify({
        "ok": True,
        "flavors": [{"id": fid, "name": f["name"], "desc": f["desc"], "cpu": f["cpu"],
                     "mem": mem_str(f["memMi"]), "disk": "%dGi" % f["diskGi"],
                     "fit": used + f["memMi"] <= MEM_BUDGET_MI} for fid, f in FLAVORS.items()],
        "memUsedMi": used, "memBudgetMi": MEM_BUDGET_MI,
        "diskUsedGi": used_disk_gi(), "diskBudgetGi": DISK_BUDGET_GI,
        "templates": [{"id": k, "name": v["name"]} for k, v in TEMPLATES.items()],
    })


# ------------------------------------------------------------------ 实例

@app.route("/api/instances")
@login_required
def list_instances(u):
    deploys = sorted(list_deploys(None if u["role"] == "admin" else u["username"]),
                     key=lambda x: x.metadata.creation_timestamp or 0)
    metrics = fetch_pod_metrics()
    disks = {p.metadata.labels.get("instance", ""): p.spec.resources.requests.get("storage", "")
             for p in core.list_namespaced_persistent_volume_claim(
                 APP_NS, label_selector="app=webide-instance").items}
    items = []
    for d in deploys:
        name = d.metadata.labels.get("instance", "")
        fid = deploy_flavor_id(d)
        f = FLAVORS[fid]
        np, extra = None, []
        try:
            svc = core.read_namespaced_service("webide-" + name, APP_NS)
            if svc.spec.type == "NodePort":
                for p in (svc.spec.ports or []):
                    if p.port == 8080:
                        np = p.node_port
                    else:
                        extra.append({"port": p.port, "nodePort": p.node_port})
        except ApiException:
            pass
        state = "已停止" if (d.spec.replicas or 0) == 0 else pod_state(name)
        m = metrics.get(name, {})
        items.append({
            "name": name, "state": state, "nodePort": np, "extraPorts": extra,
            "owner": deploy_owner(d),
            "flavor": fid, "flavorName": f["name"],
            "resources": {"cpu": f["cpu"], "mem": mem_str(f["memMi"]),
                          "disk": disks.get(name, "%dGi" % f["diskGi"])},
            "usage": {"cpu_m": m.get("cpu_m", 0), "mem_mi": m.get("mem_mi", 0)},
            "createdAt": (d.metadata.creation_timestamp.strftime("%Y-%m-%d %H:%M") if d.metadata.creation_timestamp else "-"),
        })
    return jsonify({"ok": True, "instances": items, "runningMemMi": running_mem_mi(),
                    "memBudgetMi": MEM_BUDGET_MI, "maxTotal": MAX_TOTAL,
                    "diskUsedGi": used_disk_gi(), "diskBudgetGi": DISK_BUDGET_GI})


@app.route("/api/instances", methods=["POST"])
@login_required
def create_instance(u):
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    fid = body.get("flavor") or DEFAULT_FLAVOR
    tpl = body.get("template") or "blank"
    if fid not in FLAVORS:
        return err("无效的规格套餐")
    if tpl not in TEMPLATES:
        return err("无效的初始化模板")
    f = FLAVORS[fid]
    if not NAME_RE.match(name):
        return err("实例名仅支持小写字母/数字/中划线，以字母或数字开头结尾")
    deploys = list_deploys()
    if any(d.metadata.name == "webide-" + name for d in deploys):
        return err("实例已存在")
    if len(deploys) >= MAX_TOTAL:
        return err("实例总数已达上限 %d 个" % MAX_TOTAL, 409)
    if u["role"] != "admin" and user_running_count(u["username"], deploys) >= USER_MAX_RUNNING:
        return err("每用户最多同时运行 %d 个实例" % USER_MAX_RUNNING, 409)
    used_mem = running_mem_mi(deploys)
    if used_mem + f["memMi"] > MEM_BUDGET_MI:
        return err("集群内存余量不足：需 %dMi，可用 %dMi（已用 %dMi/预算 %dMi）。请先停止部分实例或选择更低规格。"
                   % (f["memMi"], MEM_BUDGET_MI - used_mem, used_mem, MEM_BUDGET_MI), 409)
    used_disk = used_disk_gi()
    if used_disk + f["diskGi"] > DISK_BUDGET_GI:
        return err("磁盘配额不足：现有 %dGi + 新增 %dGi 超过上限 %dGi。"
                   % (used_disk, f["diskGi"], DISK_BUDGET_GI), 409)
    np = alloc_nodeport()
    if np is None:
        return err("NodePort 资源已用尽", 409)

    labels = inst_labels(name, u["username"])
    mem_req = max(256, f["memMi"] // 2)
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name="webide-ws-" + name, labels=labels),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"], storage_class_name="standard",
            resources=client.V1ResourceRequirements(requests={"storage": "%dGi" % f["diskGi"]}),
        ),
    )
    deploy = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name="webide-" + name, labels=labels,
            annotations={"webide.io/flavor": fid, "webide.io/template": tpl}),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": "webide-instance", "instance": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    init_containers=[client.V1Container(
                        name="init-home",
                        image=CODE_IMAGE,
                        command=["sh", "-c",
                                 "mkdir -p /home/coder/project && chown -R 1000:1000 /home/coder || true\n"
                                 + TEMPLATES[tpl]["script"]],
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
                            requests={"cpu": "%dm" % (int(f["cpu"]) * 100), "memory": mem_str(mem_req)},
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
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name="webide-ws-" + name),
                    )],
                ),
            ),
        ),
    )
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name="webide-" + name, labels=labels),
        spec=client.V1ServiceSpec(
            type="NodePort", selector={"app": "webide-instance", "instance": name},
            ports=[client.V1ServicePort(port=8080, target_port=8080, node_port=np)],
        ),
    )
    try:
        core.create_namespaced_persistent_volume_claim(APP_NS, pvc)
        apps.create_namespaced_deployment(APP_NS, deploy)
        core.create_namespaced_service(APP_NS, svc)
    except ApiException as e:
        return err("创建失败: %s" % e.reason, 500)
    audit(u["username"], "创建实例", name,
          "%s %s %s" % (FLAVORS[fid]["name"], TEMPLATES[tpl]["name"], ":" + str(np)))
    return jsonify({"ok": True, "name": name, "flavor": fid, "nodePort": np})


def _owner_guard(fn):
    def wrapper(u, name, *a, **kw):
        d, e = get_deploy_checked(name, u)
        if e:
            msg, code = e
            return err(msg, code)
        return fn(u, name, d, *a, **kw)

    wrapper.__name__ = fn.__name__
    return wrapper


@app.route("/api/instances/<name>", methods=["DELETE"])
@login_required
@_owner_guard
def delete_instance(u, name, d):
    try:
        apps.delete_namespaced_deployment("webide-" + name, APP_NS)
        core.delete_namespaced_service("webide-" + name, APP_NS)
        core.delete_namespaced_persistent_volume_claim("webide-ws-" + name, APP_NS)
    except ApiException as e:
        if e.status != 404:
            return err("删除失败: %s" % e.reason, 500)
    audit(u["username"], "删除实例", name)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/stop", methods=["POST"])
@login_required
@_owner_guard
def stop_instance(u, name, d):
    try:
        apps.patch_namespaced_deployment_scale("webide-" + name, APP_NS, {"spec": {"replicas": 0}})
    except ApiException as e:
        return err("停止失败: %s" % e.reason, 500)
    audit(u["username"], "停止实例", name)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/start", methods=["POST"])
@login_required
@_owner_guard
def start_instance(u, name, d):
    need = deploy_mem_mi(d)
    used = running_mem_mi()
    if used + need > MEM_BUDGET_MI:
        return err("集群内存余量不足：启动需 %dMi，可用 %dMi。" % (need, MEM_BUDGET_MI - used), 409)
    if u["role"] != "admin" and user_running_count(u["username"]) >= USER_MAX_RUNNING:
        return err("每用户最多同时运行 %d 个实例" % USER_MAX_RUNNING, 409)
    try:
        apps.patch_namespaced_deployment_scale("webide-" + name, APP_NS, {"spec": {"replicas": 1}})
    except ApiException as e:
        return err("启动失败: %s" % e.reason, 500)
    audit(u["username"], "启动实例", name)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/restart", methods=["POST"])
@login_required
@_owner_guard
def restart_instance(u, name, d):
    if (d.spec.replicas or 0) == 0:
        return err("实例未在运行，请先启动")
    try:
        apps.patch_namespaced_deployment(
            "webide-" + name, APP_NS,
            {"spec": {"template": {"metadata": {"annotations": {
                "webide.io/restartedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}}}})
    except ApiException as e:
        return err("重启失败: %s" % e.reason, 500)
    audit(u["username"], "重启实例", name)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 端口预览

@app.route("/api/instances/<name>/ports", methods=["POST"])
@login_required
@_owner_guard
def add_port(u, name, d):
    if (d.spec.replicas or 0) == 0:
        return err("实例未运行，无法暴露端口")
    body = request.get_json(silent=True) or {}
    try:
        port = int(body.get("port"))
    except (TypeError, ValueError):
        return err("端口必须是数字")
    if not (1 <= port <= 65535) or port == 8080:
        return err("端口无效（1-65535，且不能与 IDE 的 8080 冲突）")
    svc = core.read_namespaced_service("webide-" + name, APP_NS)
    ports = list(svc.spec.ports or [])
    if len([p for p in ports if p.port != 8080]) >= MAX_EXTRA_PORTS:
        return err("每实例最多暴露 %d 个应用端口" % MAX_EXTRA_PORTS)
    if any(p.port == port for p in ports):
        return err("该端口已暴露")
    np = alloc_nodeport()
    if np is None:
        return err("NodePort 资源已用尽", 409)
    ports.append(client.V1ServicePort(name="app-%d" % port, port=port, target_port=port, node_port=np))
    try:
        core.patch_namespaced_service("webide-" + name, APP_NS, {"spec": {"ports": [
            {"name": p.name or "ide", "port": p.port, "target_port": p.target_port, "node_port": p.node_port}
            for p in ports]}})
    except ApiException as e:
        return err("暴露失败: %s" % e.reason, 500)
    audit(u["username"], "暴露端口", name, "%d → NodePort %d" % (port, np))
    return jsonify({"ok": True, "port": port, "nodePort": np})


@app.route("/api/instances/<name>/ports/<int:port>", methods=["DELETE"])
@login_required
@_owner_guard
def del_port(u, name, d, port):
    svc = core.read_namespaced_service("webide-" + name, APP_NS)
    ports = [p for p in (svc.spec.ports or []) if p.port != 8080 and p.port != port]
    try:
        core.patch_namespaced_service("webide-" + name, APP_NS, {"spec": {"ports": [
            {"name": p.name or "ide", "port": p.port, "target_port": p.target_port, "node_port": p.node_port}
            for p in ports] + [{"name": "ide", "port": 8080, "target_port": 8080,
                                "node_port": [x.node_port for x in (svc.spec.ports or []) if x.port == 8080][0]}]}})
    except ApiException as e:
        return err("收回失败: %s" % e.reason, 500)
    audit(u["username"], "收回端口", name, str(port))
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 命令终端

@app.route("/api/instances/<name>/exec", methods=["POST"])
@login_required
@_owner_guard
def exec_cmd(u, name, d):
    if (d.spec.replicas or 0) == 0:
        return err("实例未运行，无法执行命令")
    cmd = ((request.get_json(silent=True) or {}).get("cmd") or "").strip()
    if not cmd:
        return err("命令为空")
    try:
        pods = core.list_namespaced_pod(
            APP_NS, label_selector="app=webide-instance,instance=" + name).items
        if not pods:
            return err("实例 Pod 不存在")
        pod = pods[0].metadata.name
        resp = k8s_exec_stream(
            core.connect_get_namespaced_pod_exec, pod, APP_NS,
            container="code-server",
            command=["/bin/sh", "-c", "timeout 25 sh -c " + shlex.quote(cmd)],
            stderr=True, stdin=False, stdout=True, tty=False,
            _preload_content=False)
        while resp.is_open():
            resp.update(timeout=25)
        out = resp.read_all()
        out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", out or "")
        return jsonify({"ok": True, "output": (out.strip() or "(无输出)")[:20000]})
    except ApiException as e:
        try:
            msg = _json.loads(e.body).get("message", "")
        except Exception:
            msg = e.reason or str(e)[:200]
        return jsonify({"ok": True, "output": "(执行失败) %s" % msg})
    except Exception as e:
        return jsonify({"ok": True, "output": "(执行超时或失败) %s" % str(e)[:400]})


# ------------------------------------------------------------------ 详情 / 监控 / 审计 / 容量

@app.route("/api/instances/<name>/detail")
@login_required
@_owner_guard
def instance_detail(u, name, d):
    fid = deploy_flavor_id(d)
    f = FLAVORS[fid]
    extra = []
    try:
        svc = core.read_namespaced_service("webide-" + name, APP_NS)
        extra = svc_extra_ports(svc)
    except ApiException:
        pass
    evs = []
    try:
        ev_list = core.list_namespaced_event(
            APP_NS, field_selector="involvedObject.name=webide-" + name)
        for e in ev_list.items[-15:]:
            ts = e.last_timestamp or e.first_timestamp or e.event_time
            evs.append({"ts": ts.strftime("%m-%d %H:%M") if ts else "-",
                        "type": e.type or "", "reason": e.reason or "", "msg": (e.message or "")[:160]})
        evs.reverse()
    except ApiException:
        pass
    m = fetch_pod_metrics().get(name, {"cpu_m": 0, "mem_mi": 0})
    with _lock:
        hist = [{"ts": h["ts"], "mem_mi": h["pods"].get(name, {}).get("mem_mi", 0)}
                for h in history if name in (h.get("pods") or {})]
    return jsonify({"ok": True, "name": name, "state": pod_state(name),
                    "owner": deploy_owner(d), "flavor": fid, "flavorName": f["name"],
                    "resources": {"cpu": f["cpu"], "mem": mem_str(f["memMi"]),
                                  "disk": "%dGi" % f["diskGi"]},
                    "nodePort": ([p.node_port for p in (
                        core.read_namespaced_service("webide-" + name, APP_NS).spec.ports or [])
                        if p.port == 8080] or [None])[0],
                    "extraPorts": extra, "usage": m, "events": evs,
                    "memCurve": hist[-120:],
                    "createdAt": (d.metadata.creation_timestamp.strftime("%Y-%m-%d %H:%M")
                                  if d.metadata.creation_timestamp else "-")})


@app.route("/api/metrics")
@login_required
def metrics(u):
    m = fetch_pod_metrics()
    with _lock:
        hist = list(history)
        hh = list(host_hist)
    return jsonify({"ok": True, "history": hist, "pods": m, "memBudgetMi": MEM_BUDGET_MI,
                    "host": dict(host_latest), "hostHistory": hh})


@app.route("/api/audit")
@login_required
@admin_required
def audit_list(u):
    conn = db()
    rows = conn.execute(
        "SELECT ts,user,action,target,detail FROM audit ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify({"ok": True, "items": [
        {"ts": r["ts"], "user": r["user"], "action": r["action"],
         "target": r["target"], "detail": r["detail"]} for r in rows]})


@app.route("/api/capacity")
@login_required
def capacity(u):
    nodes = core.list_node().items
    if not nodes:
        return err("无节点", 500)
    n = nodes[0]
    alloc = n.status.allocatable or {}
    cap = n.status.capacity or {}
    deploys = list_deploys()
    conn = db()
    users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    conn.close()
    return jsonify({
        "ok": True, "node": n.metadata.name,
        "cpuCores": cap.get("cpu", "?"), "cpuAllocatable": alloc.get("cpu", "?"),
        "memoryAllocatable": alloc.get("memory", "?"), "podCapacity": cap.get("pods", "?"),
        "instancesTotal": len(deploys), "maxTotal": MAX_TOTAL,
        "runningMemMi": running_mem_mi(deploys), "memBudgetMi": MEM_BUDGET_MI,
        "diskUsedGi": used_disk_gi(), "diskBudgetGi": DISK_BUDGET_GI,
        "userCount": users, "userMaxRunning": USER_MAX_RUNNING,
        "idleEnabled": get_setting("idle_enabled", "1") == "1",
        "idleMinutes": int(get_setting("idle_minutes", "30") or 30),
        "version": {"kubeletVersion": (n.status.node_info.kubelet_version if n.status.node_info else "?")},
    })


# ------------------------------------------------------------------ 设置 / 用户管理 (管理员)

@app.route("/api/settings")
@login_required
@admin_required
def get_settings(u):
    return jsonify({"ok": True,
                    "idleEnabled": get_setting("idle_enabled", "1") == "1",
                    "idleMinutes": int(get_setting("idle_minutes", "30") or 30)})


@app.route("/api/settings", methods=["POST"])
@login_required
@admin_required
def update_settings(u):
    body = request.get_json(silent=True) or {}
    if "idleEnabled" in body:
        set_setting("idle_enabled", "1" if body["idleEnabled"] else "0")
    if "idleMinutes" in body:
        try:
            v = max(5, min(240, int(body["idleMinutes"])))
        except (TypeError, ValueError):
            return err("空闲时长必须是数字")
        set_setting("idle_minutes", v)
    audit(u["username"], "修改系统设置", "",
          "空闲休眠 %s / %s 分钟" % (get_setting("idle_enabled", "1"), get_setting("idle_minutes", "30")))
    return jsonify({"ok": True})


@app.route("/api/users")
@login_required
@admin_required
def list_users(u):
    deploys = list_deploys()
    conn = db()
    rows = conn.execute("SELECT username, role, created_at FROM users ORDER BY created_at").fetchall()
    conn.close()
    users = []
    for r in rows:
        mine = [d for d in deploys if deploy_owner(d) == r["username"]]
        users.append({"username": r["username"], "role": r["role"],
                      "createdAt": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
                      "instances": len(mine),
                      "running": sum(1 for d in mine if (d.spec.replicas or 0) > 0)})
    return jsonify({"ok": True, "users": users})


@app.route("/api/users/<name>/reset-password", methods=["POST"])
@login_required
@admin_required
def reset_password(u, name):
    new = (request.get_json(silent=True) or {}).get("new") or ""
    if len(new) < 6:
        return err("新密码至少 6 位")
    conn = db()
    if not conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
        conn.close()
        return err("用户不存在", 404)
    salt = secrets.token_hex(8)
    conn.execute("UPDATE users SET salt=?, passhash=? WHERE username=?",
                 (salt, hash_pw(salt, new), name))
    conn.commit()
    conn.close()
    audit(u["username"], "重置用户密码", name)
    return jsonify({"ok": True})


@app.route("/api/users/<name>", methods=["DELETE"])
@login_required
@admin_required
def delete_user(u, name):
    if name == u["username"]:
        return err("不能删除自己")
    conn = db()
    if not conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
        conn.close()
        return err("用户不存在", 404)
    conn.close()
    if any(deploy_owner(d) == name for d in list_deploys()):
        return err("该用户名下仍有实例，请先删除其全部实例")
    conn = db()
    conn.execute("DELETE FROM users WHERE username=?", (name,))
    conn.commit()
    conn.close()
    audit(u["username"], "删除用户", name)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
