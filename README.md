# Flask + MongoDB on Kubernetes

A Python Flask API backed by an authenticated MongoDB database, deployed on a local Minikube cluster with persistent storage, CPU-based autoscaling and resource limits.

## Architecture

```
 Mac (localhost:5050 or minikube service URL)
        |
        v
 flask-service (NodePort 80 -> 30080)
        |
        v
 flask-app Deployment (2-5 pods, gunicorn on :5000) <-- flask-hpa (CPU 70%)
        |   host: mongodb.flask-mongodb.svc.cluster.local
        v
 mongodb Service (ClusterIP, internal only)    mongodb-headless (stable pod DNS)
        |                                             |
        +------------------+--------------------------+
                           v
              mongodb-0 (StatefulSet, auth enabled)
                           |  /data/db
                           v
              mongodb-pvc -> mongodb-pv (hostPath, 1Gi, Retain)
```

Everything runs in the `flask-mongodb` namespace. Credentials come from the `mongodb-secret` Secret, and non-sensitive settings from the `flask-config` ConfigMap.

**Stack:** Python 3.11, Flask 2.0.2, Werkzeug 2.0.3, PyMongo 3.12.0, gunicorn, MongoDB 7.0, Docker, Minikube, Kubernetes v1.37.

## Project structure

```
.
├── .env                  # Local settings, not committed and not in the image
├── .env.example          # Placeholder values
├── .gitignore
├── README.md
├── app/
│   ├── app.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .dockerignore
├── k8s/
│   ├── namespace.yaml
│   ├── mongodb-secret.yaml            # Template with placeholders
│   ├── mongodb-pv.yaml
│   ├── mongodb-pvc.yaml
│   ├── mongodb-headless-service.yaml
│   ├── mongodb-service.yaml
│   ├── mongodb-statefulset.yaml
│   ├── flask-configmap.yaml
│   ├── flask-deployment.yaml
│   ├── flask-service.yaml
│   └── flask-hpa.yaml
└── tests/
    └── load-generator.yaml            # Temporary HPA test traffic
```

## Prerequisites

Python 3.8+ (3.11 used here), pip, Docker, Minikube and kubectl. Commands below were run on macOS on Apple Silicon.

## Local setup (Part 1)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r app/requirements.txt

cp .env.example .env     # then edit the values
```

**Why use a virtual environment?** It gives each project its own interpreter and packages. This project's old pinned versions (Flask 2.0.2) can't conflict with other projects or the system Python, `requirements.txt` stays an accurate record of what the app needs, and the environment can be deleted and rebuilt at any time.

| Variable | Used by | Purpose |
|---|---|---|
| `FLASK_HOST`, `FLASK_PORT` | Local run | Bind address and port (`0.0.0.0`, `5050`) |
| `FLASK_ENV` | Local run | `development` turns on the debugger |
| `MONGODB_URI` | Local run | `mongodb://localhost:27017/` |
| `MONGODB_DATABASE`, `MONGODB_COLLECTION` | Both | `flask_db`, `data` |
| `MONGO_ROOT_USERNAME`, `MONGO_ROOT_PASSWORD` | Kubernetes Secret creation | Root credentials for in-cluster MongoDB |

Start MongoDB and the app:

```bash
docker run -d -p 27017:27017 --name mongodb -v mongodb_data:/data/db mongo:latest
python app/app.py        # http://localhost:5050
```

Port 5050 is used locally because macOS AirPlay can occupy port 5000. Use `python app/app.py` rather than `flask run`, which ignores `FLASK_PORT` and defaults to 5000. The local MongoDB runs without authentication, as in Part 1; authentication is enforced in Kubernetes.

## API

| Method and path | Description | Success | Errors |
|---|---|---|---|
| `GET /` | Welcome message with current UTC time | 200 | |
| `POST /data` | Inserts a JSON object | 201 | 400 invalid input, 503 database failure |
| `GET /data` | Returns all documents without `_id` | 200 | 503 database failure |
| `GET /health` | Readiness: pings MongoDB | 200 | 503 database unreachable |
| `GET /livez` | Liveness: process only, ignores MongoDB | 200 | |

```bash
curl http://localhost:5050/
# Welcome to the Flask app! The current time is: 2026-11-05T05:29:14.455247+00:00

curl -X POST -H "Content-Type: application/json" \
  -d '{"sampleKey":"sampleValue"}' http://localhost:5050/data
# {"status":"Data inserted"}

curl http://localhost:5050/data
# [{"sampleKey":"sampleValue"}]

curl -X POST -H "Content-Type: application/json" -d '{bad json' http://localhost:5050/data
# {"error":"A non-empty JSON object is required"}   (HTTP 400)
```

## Docker image

Build and check it:

```bash
docker build -t flask-mongodb-app:1.0 app
docker image inspect flask-mongodb-app:1.0 --format '{{.Architecture}} user={{.Config.User}}'
```

For Minikube, no registry is needed:

```bash
minikube image load flask-mongodb-app:1.0
```

To push to Docker Hub instead:

```bash
export DOCKERHUB_USER=<your-dockerhub-username>
docker login
docker tag flask-mongodb-app:1.0 $DOCKERHUB_USER/flask-mongodb-app:1.0
docker push $DOCKERHUB_USER/flask-mongodb-app:1.0

# Optional: publish for both Apple Silicon and Intel
docker buildx build --platform linux/amd64,linux/arm64 \
  -t $DOCKERHUB_USER/flask-mongodb-app:1.0 --push app
```

If you use the registry image, change `image:` in `k8s/flask-deployment.yaml` to match. The image was not pushed to a registry during testing; the tested deployment used `minikube image load`.

## Deploy on Minikube

Run from the project root, in this order:

```bash
# 1. Cluster
minikube start --driver=docker
minikube addons enable metrics-server

# 2. Image
docker build -t flask-mongodb-app:1.0 app
minikube image load flask-mongodb-app:1.0

# 3. Namespace
kubectl apply -f k8s/namespace.yaml

# 4. Secret, built from .env so no real password is stored in a manifest
( set -a; . ./.env; set +a
  kubectl create secret generic mongodb-secret -n flask-mongodb \
    --from-literal=mongo-root-username="$MONGO_ROOT_USERNAME" \
    --from-literal=mongo-root-password="$MONGO_ROOT_PASSWORD" \
    --dry-run=client -o yaml | kubectl apply -f - )

# 5. MongoDB
kubectl apply -f k8s/mongodb-pv.yaml -f k8s/mongodb-pvc.yaml
kubectl apply -f k8s/mongodb-headless-service.yaml -f k8s/mongodb-service.yaml
kubectl apply -f k8s/mongodb-statefulset.yaml
kubectl rollout status statefulset/mongodb -n flask-mongodb

# 6. Flask
kubectl apply -f k8s/flask-configmap.yaml -f k8s/flask-deployment.yaml
kubectl apply -f k8s/flask-service.yaml -f k8s/flask-hpa.yaml
kubectl rollout status deployment/flask-app -n flask-mongodb

# 7. Check
kubectl get pods,svc,pv,pvc,hpa -n flask-mongodb
```

Alternatively, edit the placeholder in `k8s/mongodb-secret.yaml` and apply that file in step 4.

### Access from your Mac

With the Docker driver on macOS, the node IP and NodePort are not directly reachable, so use one of these:

```bash
# Option A: fixed local port
kubectl port-forward svc/flask-service 5050:80 -n flask-mongodb
curl http://localhost:5050/

# Option B: NodePort through a Minikube tunnel; prints a URL and keeps running
minikube service flask-service -n flask-mongodb --url
```

## Kubernetes resources

| File | Resource | Purpose |
|---|---|---|
| `namespace.yaml` | Namespace | Isolates the project |
| `mongodb-secret.yaml` | Secret | Root username and password |
| `mongodb-pv.yaml` | PersistentVolume | 1Gi on the node at `/data/mongodb` |
| `mongodb-pvc.yaml` | PersistentVolumeClaim | Binds MongoDB to that PV |
| `mongodb-headless-service.yaml` | Headless Service | Stable DNS for `mongodb-0` |
| `mongodb-service.yaml` | ClusterIP Service | Internal database endpoint |
| `mongodb-statefulset.yaml` | StatefulSet | One authenticated MongoDB pod |
| `flask-configmap.yaml` | ConfigMap | Host, port, database and collection names |
| `flask-deployment.yaml` | Deployment | Two or more non-root Flask pods with probes |
| `flask-service.yaml` | NodePort Service | Exposes Flask outside the cluster |
| `flask-hpa.yaml` | HorizontalPodAutoscaler | 2-5 replicas at 70% CPU |

### MongoDB authentication and Secrets

The official image creates a root user from `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD`, which are injected from the Secret, and `--auth` makes MongoDB require a login.

Flask receives the same credentials as `MONGODB_USERNAME` and `MONGODB_PASSWORD`. `app.py` builds the connection string at startup, URL-escaping the values, and never logs it:

```
mongodb://<user>:<password>@mongodb.flask-mongodb.svc.cluster.local:27017/?authSource=admin
```

`authSource=admin` is needed because the root user is defined in the `admin` database, while the app's data is in `flask_db`.

**Secrets are not encrypted by base64.** Base64 is only an encoding that anyone can reverse. Protection comes from RBAC, keeping real values out of Git and, in production, enabling encryption at rest or using an external secret manager. The `.env` file is never copied into the image, so pods only receive what Kubernetes injects.

### Persistent storage

`mongodb-0` mounts `mongodb-pvc` at `/data/db`, MongoDB's data directory. The PV uses `hostPath` at `/data/mongodb`, a location Minikube keeps across restarts, with a `Retain` reclaim policy. A dedicated `mongodb-manual` storage class ensures the claim binds to this PV and not to one created by Minikube's default provisioner.

This storage lives on the single Minikube node. `minikube delete` destroys it.

## DNS resolution

**How it works.** CoreDNS runs in the cluster behind the `kube-dns` Service. The kubelet configures each pod to use it through `/etc/resolv.conf`, as captured from a test pod in this namespace:

```
search flask-mongodb.svc.cluster.local svc.cluster.local cluster.local
nameserver 10.96.0.10
options ndots:5
```

**Service names.** Every Service gets the name `<service>.<namespace>.svc.cluster.local`. Because of the search list, a pod in the same namespace can also just use `mongodb`.

**Normal and headless Services.** A ClusterIP name resolves to one stable virtual IP, and traffic is forwarded to a ready pod. A headless Service has no virtual IP, so its name resolves directly to pod IPs. Combined with a StatefulSet's `serviceName`, it also gives each pod its own name. Results observed in this cluster:

| Name | Resolves to | Type |
|---|---|---|
| `mongodb.flask-mongodb.svc.cluster.local` | `10.96.169.45` | ClusterIP virtual IP |
| `mongodb-headless.flask-mongodb.svc.cluster.local` | `10.244.0.6` | Pod IP |
| `mongodb-0.mongodb-headless.flask-mongodb.svc.cluster.local` | `10.244.0.6` | Stable name for one pod |

**How Flask finds MongoDB.**

1. The ConfigMap sets `MONGODB_HOST=mongodb.flask-mongodb.svc.cluster.local`.
2. PyMongo looks up that name, and CoreDNS returns the ClusterIP.
3. The connection is forwarded to `mongodb-0`, wherever it is running.

Pod IPs change when pods are recreated, but these names don't, so Flask needs no reconfiguration. Using `localhost` wouldn't work, because inside a Flask pod it refers to that pod's own container.

**Why ClusterIP for MongoDB.** A ClusterIP is routable only inside the cluster, so nothing outside can reach the database directly. NodePort or LoadBalancer would expose it unnecessarily.

## Resource requests and limits

Both the Flask and MongoDB containers use:

| | CPU | Memory |
|---|---|---|
| Request | 200m | 250Mi |
| Limit | 500m | 500Mi |

- **Requests** are the amount reserved for a container. The scheduler only places a pod on a node with that much unreserved capacity, so requests prevent overcommitting a node.
- **Limits** are the maximum a container can use, which stops one workload from starving others.
- **Exceeding the CPU limit** causes throttling: the container slows down but keeps running.
- **Exceeding the memory limit** gets the container OOM-killed and restarted, because memory can't be throttled.
- **HPA dependency:** CPU utilization is calculated as a percentage of the request. With a 200m request, the 70% target is 140m. Without a request, the HPA has nothing to calculate against.

**Units:** the assignment PDF specifies `250M` and `500M`, while these manifests use `250Mi` and `500Mi`. `M` is decimal (250,000,000 bytes) and `Mi` is binary (262,144,000 bytes), so the values differ by about 5%. The `Mi` form is what was tested.

MongoDB sizes its cache from available memory, so `--wiredTigerCacheSizeGB 0.25` keeps it within the 500Mi limit.

## Autoscaling

`flask-hpa` uses `autoscaling/v2` with a minimum of 2 replicas, a maximum of 5 and a 70% average CPU target. Its calculation is:

```
desiredReplicas = ceil(currentReplicas * currentUtilization / targetUtilization)
```

The default scale-down stabilization window of 5 minutes is kept, so a brief drop in traffic doesn't remove pods only for them to be needed again.

### Load-testing procedure

```bash
kubectl top nodes
kubectl top pods -n flask-mongodb

# Terminal 1: watch the HPA
kubectl get hpa flask-hpa -n flask-mongodb -w

# Terminal 2: start traffic (4 pods, 6 request loops each)
kubectl apply -f tests/load-generator.yaml

# Stop traffic, then wait about 5 minutes for scale-down
kubectl delete -f tests/load-generator.yaml
```

To send more traffic, raise the generator's replicas with `kubectl scale deployment load-generator --replicas=12 -n flask-mongodb`. In testing this still settled at 4 Flask replicas.

## Test results

These are results actually observed on this setup: macOS on Apple Silicon, Minikube with the Docker driver, Kubernetes v1.37.

| Test | Result |
|---|---|
| `GET /`, `POST /data`, `GET /data`, `GET /health` in the cluster | Passed with 200, 201, 200, 200 |
| Invalid JSON (cluster); JSON array and empty object (local) | HTTP 400 |
| Query MongoDB with no credentials | Rejected: `Command find requires authentication` |
| Log in with a wrong password | Rejected: `Authentication failed` |
| Log in with correct credentials | Returned the stored document |
| Delete pod `mongodb-0` | Data still present after it restarted |
| Delete and recreate the whole StatefulSet | Data still present |
| DNS for Service, headless Service and pod names | All resolved (see above) |
| Flask replicas | 2 of 2 ready |
| Access from Mac | Worked through port-forward and NodePort |
| Image contents | `app.py` and `requirements.txt` only, no `.env`; runs as UID 10001 |

### HPA timeline

| Time | Phase | CPU vs 70% target | Replicas | Pod CPU |
|---|---|---|---|---|
| 11:01:37 | Load started | 3% | 2 | 6-7m |
| 11:02:39 | Under load | **123%** | 2 | 246-247m |
| 11:03:20 | Scaled up | 123% (not yet refreshed) | **4** | 246-247m |
| 11:11:38 | Under load | 49% | 4 | 81-119m |
| 11:12:10 | Load removed | 49% | 4 | 81-119m |
| 11:14:46 | Metrics caught up | 7% | 4 | 13-17m |
| 11:16:50 | Waiting out window | 4% | 4 | 8-10m |
| 11:19:21 | Scaled down | 6% | **2** | 11-12m |

The HPA events recorded both changes: `New size: 4; reason: cpu resource utilization (percentage of request) above target` and `New size: 2; reason: All metrics below target`.

**Maximum replicas:** the 5-replica maximum is configured but was not reached in testing. A second, 4-minute run tripled the generator to 12 pods. CPU again hit 122%, the HPA again scaled from 2 to 4, and CPU then settled at about 59%. That is below the 70% target, so the HPA correctly had no reason to add a fifth pod.

### Issues encountered

1. **MongoDB crash loop.** An initial `--wiredTigerCacheSizeGB 0.125` failed because MongoDB's minimum is 0.25. The pod logs showed the error, and the value was corrected.
2. **`cpu: <unknown>` on the HPA.** This appeared just after creation, until metrics-server had samples for the new pods. It cleared on its own.
3. **Metrics lag.** `kubectl top` and the HPA refresh periodically, so values trail real load by up to a minute or so.
4. **Gap in test logging.** No samples were recorded between 11:03:51 and 11:09:57, and readings were lower right afterwards. The cause wasn't confirmed; the host machine pausing is the likely explanation. That period isn't included in the timeline.
5. **NodePort from macOS.** The node IP isn't reachable with the Docker driver, so `minikube service --url` or port-forward is needed.
6. **`$`-prefixed keys are accepted.** `{"$bad":1}` is stored rather than rejected on PyMongo 3.12 with the local MongoDB 9.0.4. Keys containing null bytes return 400.

## Troubleshooting

| Symptom | Check or fix |
|---|---|
| `mongodb-0` in `CrashLoopBackOff` | `kubectl logs mongodb-0 -n flask-mongodb` |
| Pod stuck after fixing a StatefulSet | Apply the fix, then `kubectl delete pod mongodb-0 -n flask-mongodb` |
| Flask pods not ready | `curl /health` returns 503 if MongoDB is unreachable or the login fails; check `kubectl logs deploy/flask-app -n flask-mongodb` |
| Auth fails after changing the Secret | The root user is only created on an empty data directory, so the old password stays valid. Change it in MongoDB, or reset the data (below) |
| `ImagePullBackOff` on Flask | Run `minikube image load flask-mongodb-app:1.0` |
| HPA shows `<unknown>` | Run `minikube addons enable metrics-server`, wait a minute, then check `kubectl top pods -n flask-mongodb` |
| PVC stuck `Pending` | Check `kubectl get pv`. A `Released` PV must be deleted and reapplied before it can bind again |
| Port 5000 busy on macOS | Local Flask uses 5050; AirPlay Receiver often holds 5000 |

## Cleanup

```bash
kubectl delete -f tests/load-generator.yaml --ignore-not-found

# Removes the app but keeps the data on the node (PV reclaim policy is Retain)
kubectl delete namespace flask-mongodb
kubectl delete pv mongodb-pv

# Permanently deletes the database files
minikube ssh -- sudo rm -rf /data/mongodb

minikube stop
```

## Design choices and alternatives

| Choice | Why | Alternative not chosen |
|---|---|---|
| StatefulSet for MongoDB | Stable identity and storage | Deployment: pods get random names, which suits stateless apps |
| One MongoDB replica | Enough for a local assignment and uses few resources | Replica set: real high availability, but needs key files and initiation, and more resources than one node justifies |
| Manual PV with `hostPath` | The assignment asks for a PV and PVC, and it works on Minikube | `volumeClaimTemplates` with the default provisioner: less to write, but the `Delete` reclaim policy on the default class is less safe for data |
| ClusterIP for Flask-to-MongoDB traffic | One stable endpoint, and internal only | Pod DNS name directly: also works, but only matters when clients must target a specific replica. The headless Service exists for the StatefulSet's pod identity |
| NodePort for Flask | Simple local access, no extra components | Ingress: needs a controller, overkill for one service. LoadBalancer: needs `minikube tunnel` |
| Credentials in a Secret, assembled in code | Keeps passwords out of images and manifests, and escapes special characters | Full URI in a ConfigMap: exposes the password. `$(VAR)` substitution in YAML: doesn't URL-escape |
| Separate `/health` and `/livez` | A MongoDB outage takes pods out of rotation without restarting them all, since a restart can't fix the database | One DB-checking endpoint for both probes: would cause needless restart loops |
| gunicorn | Production WSGI server with multiple workers | Flask's built-in server: for development only |
| `python:3.11-slim`, non-root user | Multi-arch, small and reduces risk if compromised | Alpine: can cause Python package build issues. Running as root: unnecessary |
| `mongo:7.0` pinned in the cluster | Repeatable deployments | `latest`: can change unexpectedly |
| Default HPA scale-down window | Avoids replica flapping | Shorter window: quicker demos, but less stable |
| In-cluster load generator | Tests the real Service path, with no local tools needed | `ab` or `hey` through a tunnel: the tunnel can become the bottleneck. A CPU-burning endpoint: not needed, because normal traffic triggered scaling |

## Submission checklist

- [x] Dockerfile for the Flask app
- [x] Build and push instructions
- [x] Kubernetes YAML for all resources
- [x] Deployment steps for Minikube
- [x] Flask endpoints with at least 2 replicas
- [x] MongoDB StatefulSet with authentication, and Flask connects using credentials
- [x] Flask Service reachable from the local machine
- [x] MongoDB reachable only inside the cluster
- [x] PV and PVC, with persistence tested
- [x] HPA at 70% CPU, minimum 2, maximum 5
- [x] Requests and limits set and explained (`Mi` units, see note above)
- [x] DNS explanation
- [x] Design choices and alternatives
- [x] Virtual environment benefits (cookie point)
- [x] Testing scenarios and results (cookie point)
