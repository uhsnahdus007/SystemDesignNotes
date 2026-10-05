# Chapter 23: Cloud, Containers, and Infrastructure

## Chapter card

| | |
|---|---|
| **Objective** | Understand the infrastructure layer systems actually run on: cloud building blocks (regions, AZs, shared responsibility, pricing), virtual machines vs containers, Kubernetes architecture and scheduling, resource requests/limits and their cost consequences, autoscaling behavior and flapping, spot capacity economics, infrastructure as code and GitOps, cloud networking, managed services, and the multi-AZ / multi-region / multi-cloud trade-offs. Cost behavior is measured in the lab |
| **Why this chapter exists** | Every design in this book eventually becomes instances, containers, networks, and a bill. Interviewers expect you to translate boxes-and-arrows into concrete infrastructure with sensible failure domains and cost awareness, and to know the traps (idle capacity, cross-AZ traffic, autoscaler thrash, drifted environments) |
| **Prerequisites** | Chapters 6 (scaling, autoscaling), 7 (load balancing), 11 (object storage, CDN), 18 (reliability, static stability), 21–22 (observability, security) |
| **Study time** | 10–13 hours including labs |
| **Tier** | `[GK]` throughout; scheduling and cost reasoning `[MK]` |
| **Mastery criteria** | (1) Explain regions, AZs, shared responsibility, and the IaaS/PaaS/CaaS/FaaS spectrum. (2) Compare VMs and containers (namespaces, cgroups) and their isolation. (3) Describe Kubernetes components, objects, scheduling, probes, rollouts, and disruption budgets. (4) Explain requests vs limits vs usage and the cost of over-requesting. (5) Predict HPA behavior and avoid flapping. (6) Compute spot economics and choose checkpointing. (7) Explain IaC, state, drift, and GitOps. (8) Design a VPC and estimate network costs. (9) Choose between managed services and self-hosting, and between multi-AZ, multi-region, and multi-cloud |

---

## 23.1 The Story: The Migration That Doubled the Bill

**StackLift** moves 200 services from a fleet of VMs to Kubernetes. Six months later:

1. **The bill doubled.** The cluster runs 180 nodes at 24% average real CPU usage. Teams set `requests: cpu: 2, memory: 8Gi` “to be safe”; the scheduler reserved it all; the autoscaler dutifully added nodes.
2. **Mystery restarts.** Several Java services are killed every few hours: their JVM heap (3 GB) plus off-heap memory exceeded the container's 3.5 GB memory limit (`OOMKilled`). Others are slow because the CPU limit throttles them in 100 ms bursts even though the node is idle.
3. **Autoscaler flapping.** The HPA scales the checkout service from 12 to 30 pods and back every few minutes; each scale-up triggers cold JVMs and database connection storms (Ch 6).
4. **The network bill.** Cross-AZ traffic and NAT gateway charges total $14k/month: chatty services in different zones, and every image pull goes through a NAT gateway.
5. **Drift.** Production was “fixed” by hand during an incident; three weeks later Terraform plans to destroy the manual change. Staging and production differ in 40 ways nobody can enumerate.
6. **The node upgrade.** A Kubernetes version upgrade at 14:00 drains nodes faster than pods can reschedule (no PodDisruptionBudgets); checkout is down for 11 minutes.

### Pause & Predict 23.1
> For each incident, name the mechanism (over-requesting, memory vs limit mismatch, CPU throttling, autoscaler control loop, topology-unaware routing, configuration drift, voluntary disruption) and one control. Which of these costs would exist even if StackLift had stayed on VMs?

---

## 23.2 Cloud fundamentals `[MK]`

### 23.2.1 The service spectrum
| Model | You manage | Provider manages | Examples |
|---|---|---|---|
| **IaaS** | OS, runtime, app, scaling | Hardware, virtualization, network | EC2, Compute Engine, Azure VMs |
| **CaaS** (containers as a service) | Container images, config | Servers, orchestration | ECS/Fargate, Cloud Run, Azure Container Apps |
| **Managed Kubernetes** | Workloads, node pools (partly) | Control plane | EKS, GKE, AKS |
| **PaaS** | Application code | Runtime, scaling, OS | App Engine, Heroku, Elastic Beanstalk |
| **FaaS / serverless** | Function code | Everything else, per-invocation billing | Lambda, Cloud Functions (Ch 19) |
| **SaaS** | Configuration, data | Everything | Salesforce, Datadog |
Moving up the stack reduces operational burden and flexibility; moving down increases control, effort, and (often) efficiency at scale.

### 23.2.2 Geography and failure domains
- **Region:** a geographic area with multiple data centers; independent from other regions (separate control planes mostly), latency 1–200+ ms between regions (Ch 15).
- **Availability Zone (AZ):** one or more data centers within a region with independent power, cooling, and networking; AZs in a region are connected by low-latency links (1–2 ms RTT). **The unit of failure you design around** (Ch 18: AZ-survivable capacity).
- **Edge/POPs:** CDN and DNS presence (Ch 11).
- **Scope of services:** *zonal* (a VM, an EBS volume), *regional* (a load balancer, S3, DynamoDB, managed Kubernetes control plane), *global* (IAM, Route 53, CloudFront). **A "multi-AZ" design is only as available as its least redundant dependency**: a single-AZ database or a zonal NAT gateway makes the whole stack zonal.
- **Quotas and limits:** every account has soft/hard limits (vCPUs per region, load balancers, IPs, API rate limits). Hitting them during a scale-out or failover is a classic incident; request increases ahead of time (Ch 18).

### 23.2.3 Shared responsibility
The provider secures *the cloud* (physical, hypervisor, managed-service internals); you secure *what you put in it* (identities, configuration, data, network rules, patching of your VMs/containers). Most cloud breaches are customer misconfigurations (public buckets, over-broad IAM; Ch 22).

### 23.2.4 Pricing models `[MK]`
| Model | Discount vs on-demand | Commitment/risk | Best for |
|---|---|---|---|
| **On-demand** | 0% | None | Spiky, unknown, short-term |
| **Reserved / savings plans** | ~30–70% | 1–3 years | **Steady baseline** load |
| **Spot / preemptible** | ~60–90% | Can be reclaimed with ~2 min notice | Fault-tolerant, batch, stateless with fallbacks |
| **Per-request / per-ms (serverless)** | Pay for use | Cost inverts at high steady load (Ch 19) | Spiky, event-driven |
| **Free tiers / credits** | — | Temporary | Learning |
Billing dimensions beyond compute: storage (GB-month by class), requests/operations, **data transfer** (egress to internet, cross-AZ, cross-region, NAT/processing), managed-service premiums, support plans, licenses.
**Rule of thumb for commitments:** a 1-year reservation at a 40% discount breaks even if the capacity runs ≥60% of the time; commit to the *floor* of your steady usage, not the peak.

### 23.2.5 The "well-architected" lenses
Cloud vendors' frameworks: operational excellence, security, reliability, performance efficiency, cost optimization, sustainability. Treat them as review checklists for a design (this book's Chapters 18, 21, 22, and this one correspond to them).

---

## 23.3 Virtual machines and containers `[MK/GK]`

### 23.3.1 VMs
A **hypervisor** (KVM, Xen, AWS Nitro, Hyper-V) runs multiple guest operating systems on one physical host, each with virtual CPUs, memory, disks, and NICs. Strong isolation (separate kernels), minutes-scale boot (tens of seconds with optimized images), heavy images (GBs). **Noisy neighbors** (shared caches, memory bandwidth, "steal time") and live migration pauses exist. Instances come in families (general, compute-, memory-, storage-optimized, GPU, ARM/Graviton).

### 23.3.2 Containers
A container is a **regular Linux process with isolation**, not a mini-VM:
- **Namespaces** isolate what a process *sees* (PID, network, mount, UTS, IPC, user).
- **cgroups** limit and account what it *uses* (CPU shares/quota, memory limit, I/O, PIDs).
- A **layered image** (union filesystem; OCI format) is pulled from a **registry**; layers are shared and cached, so a small final layer pulls fast.
- Containers share the host **kernel**: start in milliseconds–seconds, tiny images (MBs), high density, but weaker isolation than VMs (a kernel exploit crosses containers). For untrusted multi-tenant code use **sandboxed runtimes**: gVisor (user-space kernel) or **Firecracker microVMs** (VM isolation with container-like startup; used by AWS Lambda/Fargate).

### 23.3.3 Resource limits: two behaviors that surprise people
- **Memory limit → OOMKill.** Exceeding the cgroup memory limit gets the process killed (exit 137). Runtimes need the limit accounted for: a JVM with `-Xmx3g` also uses metaspace, thread stacks, direct buffers, and code cache, easily 20–50% more; set heap to ~60–75% of the limit (`-XX:MaxRAMPercentage=70`) and watch RSS, not only heap. (Story incident 2.)
- **CPU limit → throttling.** The CFS quota enforces `limit` CPU-time per 100 ms period; a multi-threaded service that bursts to 8 cores for 30 ms in a period with a 2-core limit is *frozen* for the rest of the period even if the node is idle, adding tens of ms of tail latency. Many teams set CPU *requests* (for scheduling) but **no CPU limits** (or generous ones) for latency-sensitive services, and always set memory limits ≈ requests.

### 23.3.4 Container best practices
Small, minimal base images (distroless/alpine), multi-stage builds, pinned digests, non-root user, read-only root filesystem, one main process per container, logs to stdout (Ch 21), **graceful shutdown on SIGTERM** (Ch 6), health probes, config via environment/files (12-factor), no secrets baked in (Ch 22), vulnerability scanning and signing, immutable tags in production. Image size drives pull time (a 1.2 GB image over a 1 Gbit/s link ≈ 10 s per node cold-start; layer caching and registries close to nodes help).

---

## 23.4 Kubernetes `[GK]`

### 23.4.1 Why an orchestrator
You declare *what you want* (3 replicas of this image, reachable at this address, with these resources); the system continuously works to make reality match: scheduling, restarts, rolling updates, scaling, service discovery, configuration, storage attachment. This is the **declarative / reconciliation-loop** model: controllers watch actual vs desired state and act.

### 23.4.2 Architecture
```text
                    ┌───────────────── control plane ─────────────────┐
 kubectl / CI ───▶  │ API server ◀──▶ etcd (Raft-replicated store, Ch 16) │
                    │   ▲      scheduler (assigns pods to nodes)          │
                    │   └─ controller manager (Deployment, ReplicaSet, Node, Job… controllers) │
                    └───────────────────────┬─────────────────────────────┘
                              watches / reports status
        ┌─────────────── node ───────────────┐   ┌────────────── node ──────────────┐
        │ kubelet · container runtime · CNI  │   │ kubelet · container runtime · CNI │
        │ kube-proxy / eBPF dataplane        │   │ pods …                            │
        │ [pod][pod][pod]                    │   │                                   │
        └────────────────────────────────────┘   └───────────────────────────────────┘
```
- **API server:** the only component that talks to etcd; all changes (and watches) go through it; authenticates and authorizes (RBAC, Ch 22).
- **etcd:** the cluster's source of truth (3 or 5 members, Ch 16); losing it loses cluster state.
- **Scheduler:** filters nodes that can't run a pod (insufficient resources, taints, affinity), scores the rest, binds the pod.
- **Controller manager:** runs the reconciliation loops.
- **kubelet:** on each node, runs the pods assigned to it, reports health, executes probes.
- **Networking (CNI):** every pod gets its own IP; pods can talk to each other without NAT (flat network; Calico, Cilium, AWS VPC CNI). **kube-proxy** (iptables/IPVS) or eBPF dataplane implements Services.
- Managed offerings (EKS/GKE/AKS) run and patch the control plane for you (a regional, replicated, SLA-backed service); you manage worker nodes (or use serverless nodes: Fargate, Autopilot).

### 23.4.3 Core objects
| Object | Purpose |
|---|---|
| **Pod** | Smallest unit: one or more containers sharing network and storage; ephemeral |
| **Deployment** → ReplicaSet | Desired replica count and rolling update strategy for stateless apps |
| **StatefulSet** | Stable identities and persistent volumes (databases, brokers); ordered rollout |
| **DaemonSet** | One pod per node (log agents, CNI, node exporters) |
| **Job / CronJob** | Run-to-completion / scheduled batch |
| **Service** (ClusterIP, NodePort, LoadBalancer) | Stable virtual IP/DNS in front of a changing set of pods |
| **Ingress / Gateway API** | L7 routing from outside (Ch 7) |
| **ConfigMap / Secret** | Configuration / sensitive data (Secrets are base64, not encrypted by default: enable encryption at rest and an external secret manager, Ch 22) |
| **PersistentVolume / PVC / StorageClass** | Durable storage (CSI drivers) |
| **Namespace, ResourceQuota, LimitRange** | Multi-tenancy boundaries and defaults |
| **ServiceAccount, Role/RoleBinding** | Identity and RBAC |
| **NetworkPolicy** | Default-deny pod-to-pod rules (zero trust, Ch 22) |
| **HorizontalPodAutoscaler** | Scale replicas on metrics |
| **PodDisruptionBudget (PDB)** | Limit voluntary disruptions (node drains/upgrades) |
| **CustomResourceDefinition (CRD) / Operator** | Extend the API; encode operational knowledge (e.g., a Postgres operator) |

### 23.4.4 Scheduling and QoS
- The scheduler places pods by **requests** (the reserved amount), not actual usage. **Requests are a promise of capacity; limits are a cap.**
- **QoS classes:** *Guaranteed* (requests = limits for CPU and memory), *Burstable* (requests < limits), *BestEffort* (none). Under node memory pressure the kubelet evicts BestEffort first, then Burstable using more than requested.
- **Placement controls:** node selectors, **affinity/anti-affinity** (co-locate or spread), **taints/tolerations** (dedicated nodes, spot nodes), **topology spread constraints** (spread replicas across zones/nodes), priority and preemption.
- **Probes** (Ch 6, 7): *startup* (slow starters), *readiness* (receive traffic?), *liveness* (restart me?). A liveness probe that checks a shared dependency can restart every pod during a DB blip (Ch 7).
- **Rolling updates:** `maxSurge`/`maxUnavailable` control the pace; combine with readiness gates, `preStop` hooks and `terminationGracePeriodSeconds` for graceful shutdown (Ch 6), and **PodDisruptionBudgets** so node drains and cluster upgrades cannot evict too many replicas at once (Story incident 6: without a PDB the drain removed all checkout pods simultaneously).

### 23.4.5 Packing, requests, and cost: measured (Lab 1, part 1)
2,000 pods, requests derived from a realistic size mix, actual usage at 10–55% of CPU request and 30–80% of memory request; nodes of 16 vCPU / 64 GiB (price proportional to size: $0.048 per vCPU-hour):
```text
total CPU requested: 929 vCPU, actually used: 293 vCPU (32% of requests); memory requested: 4680 GiB
policy / node size                                           nodes   requested CPU   requested MEM   ACTUAL CPU util   monthly cost (price proportional to size, $0.048/vCPU-hr)
first-fit in arrival order, 16 vCPU / 64 GiB nodes                74        78.5%           98.8%           24.8%         $   41,487
best-fit decreasing, 8 vCPU / 32 GiB nodes                       151        76.9%           96.9%           24.3%         $   42,328
best-fit decreasing, 16 vCPU / 64 GiB nodes                       76        76.4%           96.2%           24.1%         $   42,609
best-fit decreasing, 64 vCPU / 256 GiB nodes                      19        76.4%           96.2%           24.1%         $   42,609
best-fit decreasing, 16/64 nodes, requests right-sized to 1.3 x usage    54        45.1%           98.2%           34.0%         $   30,275
```
**Reading it carefully.**
1. **Pods used only 32% of the CPU they requested,** so even a "full" cluster (78% of CPU *requested*) is only **25% utilized in reality**: three quarters of the CPU you pay for does nothing. This is StackLift's 24%.
2. **The binding resource was memory** (98.8% of memory requested vs 78.5% of CPU): adding CPU-optimized nodes would not help; node shape (CPU:memory ratio) must match the workload's request ratio (here ≈ 1:5 vCPU:GiB).
3. **Smarter packing hardly matters; honest requests do.** First-fit vs best-fit-decreasing differ by 3%; **right-sizing requests to 1.3× real usage cut the cluster from 74 to 54 nodes (−27%, saving ≈ $11–12k/month, 29% of cost)**. The tool for this is observation: VPA recommendations, usage histograms (p95 plus headroom), Goldilocks/Kubecost-style reports, and requests reviewed per service.
4. **Node size is a blast-radius and overhead trade-off, not a cost lever** (price ∝ size, so the cost is the same): 8-vCPU nodes need 151 nodes (more per-node overhead: kubelet/daemonsets use fixed CPU/memory each, more IP addresses, more upgrade work), 64-vCPU nodes need 19 (fewer to manage, but one node failure takes 3× the pods, and bin-packing slack per node is larger). Pick sizes that fit your largest pod comfortably (>> 4:1), keep ≥ 3 nodes per AZ for N+1, and mix sizes.
Also leave **headroom**: for a node failure (N+1), for rolling updates (surge), and for the autoscaler to add nodes (minutes, Ch 6). Over-requesting buys safety at 3–4× the price; a better trade is accurate requests plus explicit headroom pods ("overprovisioning" low-priority placeholder pods that get preempted when real pods need room).

### 23.4.6 Autoscaling in Kubernetes `[GK]`
- **HPA** (horizontal): `desiredReplicas = ceil(currentReplicas × currentMetric / targetMetric)`, evaluated every ~15 s; tolerance (default 10%) suppresses changes when the ratio is near 1; **scale-up** is fast (bounded per step); **scale-down stabilization window** (default 5 minutes) takes the *highest* recommendation in the window so brief dips don't remove pods. Metrics: CPU/memory utilization relative to *requests* (so wrong requests = wrong scaling), custom metrics (requests/s per pod, queue depth via KEDA).
- **VPA** (vertical): adjusts requests (recreates pods; use in recommend mode with HPA on a different metric).
- **Cluster autoscaler / Karpenter:** adds nodes when pods are unschedulable (minutes of boot, Ch 6) and removes underused nodes; Karpenter picks instance types dynamically.
- **KEDA:** scales on event sources (queue length, Kafka lag; Ch 12/13) and to zero.
- Pods take time to become **ready** (image pull, JVM warm-up, connection pools), so the control loop has built-in delay (Ch 6).

**Measured (Lab 1, part 3).** Each pod serves 100 req/s; target utilization 60%; HPA every 15 s; 30 s to become ready; 2 hours of noisy traffic with two spikes:
```text
policy                                              scaling events   avg pods   seconds overloaded (>100% util)   pod-minutes
no stabilization, no tolerance band                       250          18.0                 107                         2,160
scale-down stabilization 300 s                             59          22.6                 107                         2,711
stabilization 300 s + 10% tolerance band                   34          22.1                 116                         2,658
```
**Reading it.** With no damping the autoscaler made **250 scaling decisions in two hours** (flapping: scale up on a noisy peak, scale down on the next dip, repeat); each is a batch of cold pods and connection churn. A 5-minute scale-down stabilization cut that to **59** and a 10% tolerance band to **34** (7× fewer), at a price of **~23% more pod-minutes** (2,711 vs 2,160): you hold capacity a little longer. Note what damping does *not* fix: **seconds overloaded stayed at ~107–116** because the overload comes from spikes where pods take 30 s to become ready; flapping control reduces churn, while spike protection needs headroom (lower target utilization, pre-scaling, queueing; Ch 6). Alert on scaling-event rate as a symptom of a mis-tuned loop.

### 23.4.7 Stateful workloads and databases
Kubernetes was designed for stateless apps. StatefulSets + persistent volumes + **operators** (CloudNativePG, Strimzi, the Redis/MongoDB operators) automate stateful systems, but you still own backup/restore, failover testing, upgrades, storage performance (network-attached volumes vs local NVMe), and zonal volume affinity (a volume lives in one AZ, so the pod must too). For most teams, **managed databases** (RDS/Aurora/Cloud SQL/DynamoDB) deliver better availability per engineering hour than self-hosting on Kubernetes.

### 23.4.8 When not to use Kubernetes
Small teams or few services; spiky low-traffic apps; no platform expertise. Alternatives: ECS/Fargate, Cloud Run/App Runner/Azure Container Apps, PaaS, or FaaS. Kubernetes shines at scale with many teams and heterogeneous workloads where a common platform and ecosystem justify the operational cost (control-plane upgrades every 3–4 months, CNI/ingress/mesh choices, security policies).

### 23.4.9 Operations
GitOps (Argo CD/Flux: the cluster pulls desired state from Git; drift is corrected automatically; audit trail), namespaces per team with quotas, default-deny NetworkPolicies, Pod Security standards (restricted), admission controllers (OPA Gatekeeper/Kyverno) to enforce resource requests, labels, images from approved registries; node image rotation and cluster upgrades via surge/blue-green node pools; multiple smaller clusters (per environment/region/team) reduce blast radius versus one giant cluster (control-plane limits ≈ 5,000 nodes / 150,000 pods per upstream guidance, but practical limits arrive earlier).

---

## 23.5 Spot and preemptible capacity `[MK]`

Spot instances are spare capacity at steep discounts that the provider can reclaim with short notice (~2 minutes on AWS; 30 s on GCP). Suitable for **fault-tolerant, interruptible work**.

**Measured (Lab 1, part 2).** A single 100-hour job on spot at 30% of the on-demand price; interruptions about every 10 hours (exponential); a checkpoint costs 2 minutes; a restart 5 minutes:
```text
(Young's rule of thumb for the best checkpoint interval: sqrt(2 x checkpoint cost x MTBF) = 49 minutes)
strategy                                       avg wall-clock (h)    p99 wall-clock (h)    avg cost (on-demand = 100)    saving
on-demand, no interruptions                           100.0                 100.0                 100.0              -
spot, no checkpoints (restart from zero)       never finishes            (>2,000 h)                     -              needs 100 h uninterrupted; P = e^-10 per attempt
spot, checkpoint every 24 h                           409.6                 931.7                 122.9              -23%
spot, checkpoint every 6 h                            138.3                 179.4                  41.5              59%
spot, checkpoint every 1 h                            109.8                 116.0                  32.9              67%
spot, checkpoint every 30 min                         110.4                 113.7                  33.1              67%
spot, checkpoint every 15 min                         115.9                 118.0                  34.8              65%
spot, checkpoint every 5 min                          142.0                 143.5                  42.6              57%
```
**Reading it.** (1) **Without checkpoints the job effectively never finishes**: surviving 100 uninterrupted hours with a 10-hour mean time between interruptions has probability e⁻¹⁰ ≈ 0.005% per attempt. (2) Checkpointing too rarely (24 h) is *worse than on-demand* (123 vs 100: you keep redoing lost work). (3) The savings curve is a shallow bowl: **a checkpoint every 30–60 minutes** (Young's formula says 49 min) yields **a 67% saving** with only 10% extra wall-clock time and a tight p99 (114–116 h). (4) Over-checkpointing (every 5 min) wastes enough time (142 h) to cut the saving to 57%.
**Practice:** use spot for stateless replicas behind an on-demand baseline, batch/ETL, CI runners, ML training with frequent checkpoints, and workers consuming queues (Ch 12) with idempotent tasks. Diversify across instance types and AZs (reclaims are correlated within a pool), handle the interruption notice (stop accepting work, drain, checkpoint), keep an on-demand fallback node group, and never put quorum members, primaries, or singletons on spot.

---

## 23.6 Infrastructure as code (IaC) and delivery `[MK/GK]`

### 23.6.1 Why
Clicking in a console produces unrepeatable, unreviewable, drifting environments (Story incident 5). **Infrastructure as code** describes infrastructure in version-controlled files: reviewable, testable, repeatable, auditable, and re-creatable after a disaster (the *infrastructure* DR plan).
- **Declarative tools:** Terraform/OpenTofu, CloudFormation, Pulumi (general-purpose languages), CDK (code that generates CloudFormation), Crossplane (Kubernetes-native). You declare the end state; the tool computes a **plan** (diff between desired, recorded **state**, and reality) and applies it.
- **State:** Terraform's state file maps code to real resources; keep it in a **remote backend with locking** (S3+DynamoDB, GCS, Terraform Cloud); treat it as sensitive (it contains secrets); split state per environment/component to limit blast radius and lock contention.
- **Drift:** reality differing from code (manual changes). Detect with scheduled `plan`; remediate by importing/reverting; restrict console write access (break-glass only).
- **Modules** for reusable, versioned building blocks (a "VPC," a "service") that encode org standards (tags, encryption, logging).
- **Safe delivery of infrastructure:** review the **plan** in the PR, policy-as-code checks (OPA/Conftest, Sentinel, Checkov/tfsec: no public buckets, mandatory encryption), staged applies (dev → staging → prod, one region/cell at a time), protect stateful resources (`prevent_destroy`, deletion protection), and keep apply permissions in CI (OIDC federation, short-lived credentials; Ch 22), not on laptops.
- **Immutable infrastructure:** replace servers/images rather than patching in place (golden AMIs, container images); roll back by redeploying the previous version.
- **GitOps:** Git is the source of truth for cluster/app configuration; a controller reconciles (Ch 18: progressive delivery).
- **Environments:** separate cloud accounts/projects for prod and non-prod (blast radius, IAM, billing), identical code with parameter files; avoid snowflake environments.
- **Internal developer platforms (IDPs):** golden-path templates and self-service portals (Backstage, Port) so product teams get compliant infrastructure without tickets (Ch 19: platform teams).

---

## 23.7 Cloud networking `[MK]`

### 23.7.1 VPC design
A **Virtual Private Cloud** is your isolated network in a region: a CIDR block (e.g., `10.0.0.0/16` = 65,536 addresses), split into **subnets** per AZ (e.g., `/20` = 4,096 addresses, 5 reserved by the provider).
```text
 VPC 10.0.0.0/16
 ├─ public subnets  (per AZ)  → route to Internet Gateway: load balancers, NAT gateways, bastions (avoid)
 ├─ private app subnets (per AZ) → route to NAT gateway for outbound; no inbound from internet: services, Kubernetes nodes
 └─ isolated data subnets (per AZ) → no internet route: databases, caches
```
- **Security groups** (stateful, attached to instances/ENIs; allow rules referencing other groups) and **network ACLs** (stateless, subnet-level) provide layered filtering.
- **CIDR planning:** non-overlapping ranges across VPCs, accounts, regions, and on-prem (for peering/VPN); leave room to grow; with the AWS VPC CNI each pod consumes a VPC IP, so large clusters exhaust subnets quickly (plan /19–/20 pod subnets or use overlay/prefix delegation).
- **Connectivity:** VPC peering (non-transitive), **Transit Gateway** (hub-and-spoke), **PrivateLink/VPC endpoints** (private access to cloud services or other VPCs' services without internet routes; also cuts NAT costs), site-to-site VPN and Direct Connect/ExpressRoute for hybrid.
- **DNS:** private hosted zones, split-horizon, service discovery; keep TTLs sensible (Ch 7).
- **Edge:** CDN, WAF, DDoS protection, global load balancers (Ch 7, 11).
- **Egress control:** NAT gateways (per-AZ for resilience), proxies, firewall allowlists (Ch 22).

### 23.7.2 Network cost arithmetic (Lab 1, part 4)
Illustrative list prices: cross-AZ $0.01/GB in each direction, NAT gateway processing $0.045/GB, internet egress $0.09/GB:
```text
service A -> service B at 200 MB/s = 518,400 GB/month
  spread evenly over 3 AZs, random routing: 2/3 crosses an AZ boundary -> 345,600 GB x $0.02 (out+in) = $6,912/month
  zone-aware routing keeps 90% of calls in-zone: 51,840 GB crossing -> $1,037/month  (saves $5,875)
  50 TB/month of container image pulls and API calls through a NAT gateway: $2,250/month processing; VPC endpoints/private access remove most of it
  100 TB/month served directly from the origin to the internet: $9,000; if a CDN absorbs 95% of it: origin egress $450 (CDN fees separate)
```
One chatty service pair costs **$6.9k/month** in cross-AZ charges with random load balancing; **topology-aware routing** (prefer same-zone endpoints, Kubernetes topology-aware routing / zone-aware gateways) saves **$5.9k/month** (85%) while keeping cross-AZ failover available. Other levers: place heavy data movers in the same AZ as their data, use gateway/interface endpoints for S3/DynamoDB/ECR, compress and batch, avoid cross-region replication unless required, and use the CDN for egress. **Design principle:** *data gravity*: move compute to data, not data to compute; and remember that availability (spreading across AZs) trades against network cost and latency.

---

## 23.8 Managed services vs self-hosting `[MK]`

| Dimension | Managed (RDS, MSK, ElastiCache, DynamoDB, Pub/Sub) | Self-hosted on VMs/K8s |
|---|---|---|
| Time to value | Minutes | Weeks to build competence |
| Operations (patching, backups, failover, scaling) | Provider | You |
| Availability | Provider SLA, multi-AZ built in (verify!) | As good as your engineering |
| Cost | Premium per unit (often 1.3–3× raw compute) | Cheaper at large, steady scale if you have the staff |
| Control/tuning/extensions | Limited | Full |
| Lock-in | API/behavior/egress lock-in | Portable (but operational lock-in to your own work) |
| Compliance/data residency | Check regions/certifications | Full control |
**Default:** use managed services for undifferentiated heavy lifting (databases, queues, caches, object storage, identity) and spend engineering time on what differentiates the product; self-host when scale economics, unusual requirements, or portability clearly justify the people cost. Count **engineer-hours** in TCO: an on-call rotation for a self-hosted Kafka cluster costs more than the managed premium for most organizations.
**Lock-in is a spectrum:** compute is portable (containers), data and proprietary services are not (DynamoDB, BigQuery, Spanner, Lambda triggers); the real exit cost is **data egress plus rewrite**. Mitigate with open protocols (Postgres, Kafka, S3 API, OpenTelemetry) where cheap, accept lock-in where the benefit is large.

---

## 23.9 Multi-AZ, multi-region, and multi-cloud `[MK/GK]`

- **Multi-AZ** is the default for production: stateless tiers across ≥ 3 AZs with AZ-survivable capacity; databases with synchronous standbys in another AZ (Ch 9); load balancers cross-zone (Ch 7); topology spread constraints; **avoid hidden single-AZ dependencies** (a zonal NAT gateway, an EBS volume, an in-AZ cache).
- **Multi-region** for disaster recovery, latency, or regulatory residency: active–passive (warm standby) or active–active (Ch 9, 15, 18). Costs: data replication and consistency (WAN RTT per linearizable write, Ch 15), duplicate capacity, operational complexity, testing. Plan **static stability**: failover must not depend on a control plane that may be impaired in the failed region (Ch 18); global services (IAM, DNS) can themselves be shared fate.
- **Multi-cloud** (workloads across providers): motivations: regulation or customer demand, negotiating leverage, avoiding provider-wide outages, acquisitions. Costs: **lowest-common-denominator** services, duplicated tooling and skills, egress charges between clouds, inconsistent IAM/networking, doubled security surface, and complex failover that is rarely tested. Honest assessment: **multi-region within one provider usually gives more resilience per unit of complexity than multi-cloud.** Portability layers (Kubernetes, Terraform, OpenTelemetry, Postgres/Kafka) reduce but do not remove lock-in; design a credible *exit plan* for critical data even if you never use it.
- **Hybrid/edge:** on-prem or edge sites connected via VPN/Direct Connect; data sovereignty; latency-sensitive local processing; Kubernetes distributions at the edge; keep a clear source of truth and sync strategy (Ch 20).
- **Control-plane vs data-plane failure:** providers' regional outages historically involve control planes (launching instances, IAM, DNS); running workloads often keep working. Design so steady-state serving does not require control-plane calls (pre-provisioned capacity, cached credentials, no dependence on autoscaling during an incident).

---

## 23.10 Choosing the compute model `[MK]`

| Need | Prefer |
|---|---|
| Few services, small team, simple scaling | PaaS or CaaS (Cloud Run, Fargate, App Runner) |
| Many services, many teams, heterogeneous workloads | Managed Kubernetes + platform team |
| Spiky/event-driven glue | FaaS (Ch 19) |
| Steady high-throughput, performance-sensitive | VMs or containers on reserved capacity, tuned |
| GPU/ML training/batch | Spot/preemptible with checkpointing; batch schedulers |
| Strong isolation for untrusted code | microVMs (Firecracker), gVisor, dedicated tenancy |
| Stateful databases | Managed DB first; operators on K8s if necessary; avoid network-attached storage for latency-critical stores |
| Lowest ops burden | SaaS/managed services |
Typical fit with earlier chapters: stateless tier (Ch 6) on CaaS/K8s with HPA; workers (Ch 12) on spot with KEDA; databases (Ch 9, 10) managed; CDN/object storage (Ch 11) managed; Kafka (Ch 13) managed or operator-run with dedicated nodes; observability (Ch 21) hybrid.

---

## 23.11 Operating the platform `[MK]`

- **Account/project structure:** landing zone with organization → environments → workloads accounts; centralized logging/security accounts; billing separation; guardrails via service control policies (Ch 22).
- **Tagging and cost allocation:** mandatory tags (team, service, env, cost-center); **showback/chargeback** so teams see their spend; budgets and anomaly alerts; unit economics (cost per request, per tenant, per GB processed). This is **FinOps**: cost as a design constraint reviewed with the same discipline as latency.
- **Patching and upgrades:** rotate nodes regularly from fresh images; upgrade Kubernetes in a staged manner (non-prod → canary cluster → prod) with PDBs, surge nodes, and tested rollback; deprecation of APIs checked in CI.
- **Capacity and quotas:** track quota usage; keep headroom for failover (AZ loss) and scaling (Ch 18); reserve capacity for launches.
- **Resilience of the platform itself:** multiple clusters across AZs/regions; avoid a single shared ingress/DNS/CI path as a global SPOF; backups of etcd/IaC state; rehearse cluster rebuild from code.
- **Observability and security baselines** (Ch 21, 22) built into the platform templates.
- **Sustainability:** right-sizing, scheduling on efficient (ARM) instances, region choice, and turning off idle environments reduce cost and carbon together.

---

## 23.12 Failure modes and anti-patterns

| Anti-pattern | Symptom | Fix |
|---|---|---|
| **Over-requesting resources** | 25% real utilization, 2× bill (lab: 32% of requests used) | Right-size requests from p95 usage + headroom; VPA recommendations; quotas |
| **No requests set** | BestEffort pods evicted; bad scheduling | Admission policy requiring requests |
| **Memory limit below real footprint** | OOMKilled loops | Account for non-heap memory; limits ≈ requests; watch RSS |
| **Tight CPU limits** | Throttling, high p99 | Remove or raise CPU limits for latency-sensitive services |
| **Autoscaler flapping** | 250 scale events in 2 h (lab) | Stabilization window, tolerance, better metric, headroom |
| **Scaling on CPU for I/O-bound apps** | No scaling when needed | Scale on RPS/queue lag/latency |
| **Liveness probe checks dependencies** | Mass restarts during blips | Local-only liveness; readiness for traffic |
| **No PDBs / graceful shutdown** | Outage during drains/upgrades | PDBs, `preStop`, termination grace |
| **Single-AZ dependencies** | "Multi-AZ" app is zonal | Dependency audit; per-AZ NAT; regional services |
| **Random cross-AZ routing** | $6.9k/month for one pair (lab) | Topology-aware routing |
| **NAT gateway for everything** | Surprise bills | VPC endpoints, caching registries |
| **Spot without checkpoints** | Jobs never finish; wasted spend | Checkpoint every ~Young interval; diversify; fallback |
| **Quorum/primary on spot** | Outages on reclaim | On-demand for stateful and control components |
| **Click-ops and drift** | Unreproducible environments | IaC, drift detection, restricted console access |
| **Shared Terraform state for everything** | Slow, risky applies | Split state; modules; staged rollout |
| **Secrets in manifests/state** | Leaks | Secret manager, sealed/external secrets, encrypted state |
| **One giant cluster** | Blast radius, upgrade risk | Multiple clusters per env/region |
| **Unbounded namespaces** | Noisy neighbors | ResourceQuota, LimitRange, NetworkPolicy |
| **Latest tags / mutable images** | Unreproducible deploys | Pin digests, signed images |
| **Ignoring quotas** | Failover fails to scale | Quota monitoring and pre-raised limits |
| **Premature multi-cloud** | Complexity, no resilience | Multi-region on one provider; exit plan |
| **Self-hosting commodity services** | Pager burden | Managed services by default |

---

## 23.13 Labs

### Lab 1: Scheduler packing, spot economics, HPA flapping, and network cost arithmetic (Java 17+)
```java
import java.util.*;

public class CloudLab {
    static double exp(Random r, double mean) { return -Math.log(1 - r.nextDouble()) * mean; }

    // ====================================================================== 1) scheduler bin packing and requests vs usage
    record Pod(double cpuReq, double memReq, double cpuUse, double memUse) {}
    static class Node { double cpu = 16, mem = 64, usedCpu, usedMem, useCpu, useMem; int pods; boolean fits(Pod p) { return usedCpu + p.cpuReq() <= cpu && usedMem + p.memReq() <= mem; } void add(Pod p) { usedCpu += p.cpuReq(); usedMem += p.memReq(); useCpu += p.cpuUse(); useMem += p.memUse(); pods++; } }

    static void scheduler() {
        Random r = new Random(3); int n = 2000; double[] sizes = {0.1, 0.25, 0.5, 1, 2}, w = {0.30, 0.30, 0.20, 0.15, 0.05}; List<Pod> pods = new ArrayList<>(); double totCpuUse = 0;
        for (int i = 0; i < n; i++) { double u = r.nextDouble(), c = 0.5, cum = 0; for (int k = 0; k < sizes.length; k++) { cum += w[k]; if (u <= cum) { c = sizes[k]; break; } }
            double ratio = 0.5 + r.nextDouble() * 4; double mem = c * ratio * 2;                       // requested GiB
            double cpuUse = c * (0.10 + r.nextDouble() * 0.45), memUse = mem * (0.30 + r.nextDouble() * 0.5); pods.add(new Pod(c, mem, cpuUse, memUse)); totCpuUse += cpuUse; }
        System.out.println("== 1) Scheduling 2,000 pods onto 16 vCPU / 64 GiB nodes. Requests are what the scheduler reserves; usage is what the pods really consume. ==");
        double sumReq = pods.stream().mapToDouble(Pod::cpuReq).sum(), sumMemReq = pods.stream().mapToDouble(Pod::memReq).sum();
        System.out.printf("total CPU requested: %.0f vCPU, actually used: %.0f vCPU (%.0f%% of requests); memory requested: %.0f GiB%n", sumReq, totCpuUse, 100 * totCpuUse / sumReq, sumMemReq);
        System.out.println("policy / node size                                           nodes   requested CPU   requested MEM   ACTUAL CPU util   monthly cost (price proportional to size, $0.048/vCPU-hr)");
        record Cfg(String name, double cpu, double mem, boolean sorted, boolean rightSize) {}
        for (Cfg c : new Cfg[]{new Cfg("first-fit in arrival order, 16 vCPU / 64 GiB nodes", 16, 64, false, false), new Cfg("best-fit decreasing, 8 vCPU / 32 GiB nodes", 8, 32, true, false), new Cfg("best-fit decreasing, 16 vCPU / 64 GiB nodes", 16, 64, true, false),
                               new Cfg("best-fit decreasing, 64 vCPU / 256 GiB nodes", 64, 256, true, false), new Cfg("best-fit decreasing, 16/64 nodes, requests right-sized to 1.3 x usage", 16, 64, true, true)}) {
            List<Pod> list = new ArrayList<>(pods);
            if (c.rightSize()) { List<Pod> rs = new ArrayList<>(); for (Pod q : pods) rs.add(new Pod(Math.max(0.05, q.cpuUse() * 1.3), Math.max(0.25, q.memUse() * 1.3), q.cpuUse(), q.memUse())); list = rs; }
            if (c.sorted()) list.sort((x, y) -> Double.compare(y.cpuReq() / c.cpu() + y.memReq() / c.mem(), x.cpuReq() / c.cpu() + x.memReq() / c.mem()));
            List<Node> nodes = new ArrayList<>();
            for (Pod pod : list) {
                Node best = null;
                for (Node nd : nodes) { if (nd.usedCpu + pod.cpuReq() <= c.cpu() && nd.usedMem + pod.memReq() <= c.mem()) { if (!c.sorted()) { best = nd; break; } double slack = (c.cpu() - nd.usedCpu) / c.cpu() + (c.mem() - nd.usedMem) / c.mem(); double bs = best == null ? 1e9 : (c.cpu() - best.usedCpu) / c.cpu() + (c.mem() - best.usedMem) / c.mem(); if (slack < bs) best = nd; } }
                if (best == null) { best = new Node(); best.cpu = c.cpu(); best.mem = c.mem(); nodes.add(best); }
                best.add(pod);
            }
            double reqCpu = 0, reqMem = 0, useCpu = 0; for (Node nd : nodes) { reqCpu += nd.usedCpu; reqMem += nd.usedMem; useCpu += nd.useCpu; }
            System.out.printf("%-62s %5d       %5.1f%%          %5.1f%%          %5.1f%%         $%,9.0f%n", c.name(), nodes.size(), 100 * reqCpu / (nodes.size() * c.cpu()), 100 * reqMem / (nodes.size() * c.mem()), 100 * useCpu / (nodes.size() * c.cpu()), nodes.size() * c.cpu() * 0.048 * 730);
        }
    }

    // ====================================================================== 2) spot instances with checkpointing
    static void spot() {
        double work = 100, mtbf = 10, ckptCost = 2.0 / 60, restart = 5.0 / 60, spotPrice = 0.30, onDemand = 1.0; int runs = 3000; Random r = new Random(5);
        System.out.println("\n== 2) Spot instances. A single 100-hour job; spot price = 30% of on-demand; interruptions arrive every ~10 h on average (exponential); each checkpoint costs 2 min; a restart costs 5 min. ==");
        System.out.println("(Young's rule of thumb for the best checkpoint interval: sqrt(2 x checkpoint cost x MTBF) = " + String.format("%.0f", Math.sqrt(2 * ckptCost * mtbf) * 60) + " minutes)");
        System.out.println("strategy                                       avg wall-clock (h)    p99 wall-clock (h)    avg cost (on-demand = 100)    saving    ");
        System.out.printf("%-46s %12.1f          %12.1f          %12.1f              %s%n", "on-demand, no interruptions", work, work, work * onDemand, "-");
        double[] intervals = {Double.POSITIVE_INFINITY, 24, 6, 1, 0.5, 0.25, 5.0 / 60};
        String[] names = {"spot, no checkpoints (restart from zero)", "spot, checkpoint every 24 h", "spot, checkpoint every 6 h", "spot, checkpoint every 1 h", "spot, checkpoint every 30 min", "spot, checkpoint every 15 min", "spot, checkpoint every 5 min"};
        for (int s = 0; s < intervals.length; s++) {
            double[] wall = new double[runs]; double costSum = 0; double cap = 2000;
            for (int i = 0; i < runs; i++) {
                double done = 0, saved = 0, t = 0;
                while (done < work && t < cap) {
                    double toFail = exp(r, mtbf), seg = Math.min(intervals[s], work - saved);          // work between checkpoints
                    double need = seg + (saved + seg < work ? ckptCost : 0);
                    if (toFail >= need) { t += need; saved += seg; done = saved; }                     // segment completes and is saved
                    else { t += toFail + restart; }                                                    // interrupted: lose the unsaved segment, pay a restart delay
                }
                wall[i] = t; costSum += t * spotPrice;
            }
            Arrays.sort(wall); double avg = Arrays.stream(wall).average().orElse(0);
            if (wall[runs / 2] >= cap) { System.out.printf("%-46s %12s          %12s          %12s              %s%n", names[s], "never finishes", "(>2,000 h)", "-", "needs 100 h uninterrupted; P = e^-10 per attempt"); continue; }
            System.out.printf("%-46s %12.1f          %12.1f          %12.1f              %.0f%%%n", names[s], avg, wall[(int) (runs * 0.99)], costSum / runs, 100 * (1 - costSum / runs / (work * onDemand)));
        }
    }

    // ====================================================================== 3) HPA flapping
    static void hpa() {
        System.out.println("\n== 3) Horizontal Pod Autoscaler: flapping vs scale-down stabilization. Each pod serves 100 req/s; target CPU utilization 60%; evaluation every 15 s; new pods take 30 s to become ready. 2 hours of noisy traffic with two spikes. ==");
        System.out.println("policy                                              scaling events   avg pods   seconds overloaded (>100% util)   pod-minutes");
        for (int variant = 0; variant < 3; variant++) {
            Random r = new Random(8); int T = 7200; double[] load = new double[T];
            for (int t = 0; t < T; t++) { double base = 1000 + 300 * Math.sin(t / 400.0) + r.nextGaussian() * 120; if (t >= 1800 && t < 2100) base += 1500; if (t >= 4500 && t < 4620) base += 2200; load[t] = Math.max(100, base); }
            int stabilization = variant == 0 ? 0 : 300; double tolerance = variant == 2 ? 0.10 : 0.0;
            int ready = 14; List<int[]> starting = new ArrayList<>(); Deque<int[]> history = new ArrayDeque<>(); int events = 0; double podSec = 0; int overloaded = 0; int target = ready;
            for (int t = 0; t < T; t++) {
                for (Iterator<int[]> it = starting.iterator(); it.hasNext(); ) { int[] s = it.next(); if (s[0] <= t) { ready += s[1]; it.remove(); } }
                double util = load[t] / (ready * 100.0); if (util > 1.0) overloaded++; podSec += ready;
                if (t % 15 == 0) {
                    double avgLoad = 0; int c = 0; for (int k = Math.max(0, t - 14); k <= t; k++) { avgLoad += load[k]; c++; } avgLoad /= c; double u = avgLoad / (Math.max(1, ready) * 100.0);   // metric = average over the last 15 s
                    int inflight = starting.stream().mapToInt(a -> a[1]).sum(); int current = ready + inflight;
                    double ratio = u / 0.60; int desired = (int) Math.ceil(ready * ratio); desired = Math.min(desired, 2 * ready);               // scale up at most 2x per decision
                    if (Math.abs(ratio - 1.0) <= tolerance) desired = current;                                                                  // tolerance band: do nothing when close to target
                    history.addLast(new int[]{t, desired}); while (!history.isEmpty() && history.peekFirst()[0] < t - stabilization) history.pollFirst();
                    int finalDesired = desired; if (desired < current) { int mx = 0; for (int[] h : history) mx = Math.max(mx, h[1]); finalDesired = Math.min(current, Math.max(desired, mx)); }   // scale down only to the highest recommendation in the window
                    if (finalDesired > current) { starting.add(new int[]{t + 30, finalDesired - current}); events++; }
                    else if (finalDesired < current) { int remove = Math.min(ready - 1, current - finalDesired); ready -= remove; events++; }
                }
            }
            String[] names = {"no stabilization, no tolerance band", "scale-down stabilization 300 s", "stabilization 300 s + 10% tolerance band"};
            System.out.printf("%-52s %8d        %6.1f              %6d                      %,8.0f%n", names[variant], events, podSec / T, overloaded, podSec / 60);
        }
    }

    // ====================================================================== 4) cost arithmetic: network and topology
    static void networkCosts() {
        System.out.println("\n== 4) Network cost arithmetic (illustrative list prices: cross-AZ $0.01/GB each direction, NAT gateway processing $0.045/GB, internet egress $0.09/GB) ==");
        double mbps = 200; double secMonth = 30 * 86400; double gb = mbps / 1000 * secMonth;
        System.out.printf("service A -> service B at %.0f MB/s = %,.0f GB/month%n", mbps, gb);
        System.out.printf("  spread evenly over 3 AZs, random routing: 2/3 crosses an AZ boundary -> %,.0f GB x $0.02 (out+in) = $%,.0f/month%n", gb * 2 / 3, gb * 2 / 3 * 0.02);
        System.out.printf("  zone-aware routing keeps 90%% of calls in-zone: %,.0f GB crossing -> $%,.0f/month  (saves $%,.0f)%n", gb * 0.1, gb * 0.1 * 0.02, gb * 2 / 3 * 0.02 - gb * 0.1 * 0.02);
        double natTb = 50; System.out.printf("  50 TB/month of container image pulls and API calls through a NAT gateway: $%,.0f/month processing; VPC endpoints/private access remove most of it%n", natTb * 1000 * 0.045);
        double egressTb = 100; System.out.printf("  100 TB/month served directly from the origin to the internet: $%,.0f; if a CDN absorbs 95%% of it: origin egress $%,.0f (CDN fees separate)%n", egressTb * 1000 * 0.09, egressTb * 1000 * 0.09 * 0.05);
    }

    public static void main(String[] a) { scheduler(); spot(); hpa(); networkCosts(); }
}
```
**Sample output**
```text
== 1) Scheduling 2,000 pods onto 16 vCPU / 64 GiB nodes. Requests are what the scheduler reserves; usage is what the pods really consume. ==
total CPU requested: 929 vCPU, actually used: 293 vCPU (32% of requests); memory requested: 4680 GiB
policy / node size                                           nodes   requested CPU   requested MEM   ACTUAL CPU util   monthly cost (price proportional to size, $0.048/vCPU-hr)
first-fit in arrival order, 16 vCPU / 64 GiB nodes                74        78.5%           98.8%           24.8%         $   41,487
best-fit decreasing, 8 vCPU / 32 GiB nodes                       151        76.9%           96.9%           24.3%         $   42,328
best-fit decreasing, 16 vCPU / 64 GiB nodes                       76        76.4%           96.2%           24.1%         $   42,609
best-fit decreasing, 64 vCPU / 256 GiB nodes                      19        76.4%           96.2%           24.1%         $   42,609
best-fit decreasing, 16/64 nodes, requests right-sized to 1.3 x usage    54        45.1%           98.2%           34.0%         $   30,275

== 2) Spot instances. A single 100-hour job; spot price = 30% of on-demand; interruptions arrive every ~10 h on average (exponential); each checkpoint costs 2 min; a restart costs 5 min. ==
(Young's rule of thumb for the best checkpoint interval: sqrt(2 x checkpoint cost x MTBF) = 49 minutes)
strategy                                       avg wall-clock (h)    p99 wall-clock (h)    avg cost (on-demand = 100)    saving    
on-demand, no interruptions                           100.0                 100.0                 100.0              -
spot, no checkpoints (restart from zero)       never finishes            (>2,000 h)                     -              needs 100 h uninterrupted; P = e^-10 per attempt
spot, checkpoint every 24 h                           409.6                 931.7                 122.9              -23%
spot, checkpoint every 6 h                            138.3                 179.4                  41.5              59%
spot, checkpoint every 1 h                            109.8                 116.0                  32.9              67%
spot, checkpoint every 30 min                         110.4                 113.7                  33.1              67%
spot, checkpoint every 15 min                         115.9                 118.0                  34.8              65%
spot, checkpoint every 5 min                          142.0                 143.5                  42.6              57%

== 3) Horizontal Pod Autoscaler: flapping vs scale-down stabilization. Each pod serves 100 req/s; target CPU utilization 60%; evaluation every 15 s; new pods take 30 s to become ready. 2 hours of noisy traffic with two spikes. ==
policy                                              scaling events   avg pods   seconds overloaded (>100% util)   pod-minutes
no stabilization, no tolerance band                       250          18.0                 107                         2,160
scale-down stabilization 300 s                             59          22.6                 107                         2,711
stabilization 300 s + 10% tolerance band                   34          22.1                 116                         2,658

== 4) Network cost arithmetic (illustrative list prices: cross-AZ $0.01/GB each direction, NAT gateway processing $0.045/GB, internet egress $0.09/GB) ==
service A -> service B at 200 MB/s = 518,400 GB/month
  spread evenly over 3 AZs, random routing: 2/3 crosses an AZ boundary -> 345,600 GB x $0.02 (out+in) = $6,912/month
  zone-aware routing keeps 90% of calls in-zone: 51,840 GB crossing -> $1,037/month  (saves $5,875)
  50 TB/month of container image pulls and API calls through a NAT gateway: $2,250/month processing; VPC endpoints/private access remove most of it
  100 TB/month served directly from the origin to the internet: $9,000; if a CDN absorbs 95% of it: origin egress $450 (CDN fees separate)
```
**Experiments**
1. In part 1, change the pod size mix and usage ratios (usage at 60–90% of requests) and see when right-sizing stops paying off. Add **three node shapes** (CPU-optimized 1:2, general 1:4, memory-optimized 1:8 vCPU:GiB) and choose per pod the cheapest feasible shape; compare total cost.
2. In part 1, model **headroom**: reserve 20% of each node and run an N+1 AZ-loss scenario (3 AZs). How many extra nodes are required? Add placeholder low-priority pods and show how they absorb scale-up latency.
3. In part 2, change the spot interruption rate (MTBF 2 h, 10 h, 50 h) and the checkpoint cost (30 s, 2 min, 10 min); plot the optimal interval and compare with Young's formula. Add **parallel workers** (10 spot workers each doing 10 h; job done when all finish) and see how the tail latency of the slowest worker changes the economics.
4. In part 3, add **CPU request mismatch**: pods request 1 vCPU but use 0.2; HPA scales on utilization relative to requests. Show the autoscaler under-scales for the actual load; then switch the metric to requests/s per pod.
5. In part 3, implement `behavior` policies (scale up max +100% per minute, scale down max 10% per minute) and a **predictive pre-scale** for the known spike; measure overloaded seconds.
6. In part 4, extend the arithmetic to a full architecture (ingress → 5 services → database across 3 AZs, 2 regions with replication): estimate monthly network cost by hop and propose the three biggest savings.

### Lab 2: A deployment done right on a local cluster (needs Docker + kind/minikube; not executed in the book's sandbox)
Deploy the Chapter 2 `TaskApi` as a `Deployment` with 3 replicas: set CPU/memory **requests** from measured usage and a memory limit; add readiness/liveness/startup probes; `preStop` sleep + `terminationGracePeriodSeconds`; a `PodDisruptionBudget` (`minAvailable: 2`); a `HorizontalPodAutoscaler` (CPU target 60%, scale-down stabilization 300 s); `topologySpreadConstraints` across nodes; a `NetworkPolicy` default-deny. Then (1) generate load and watch the HPA; (2) `kubectl drain` a node and verify the PDB prevents going below 2 ready replicas; (3) set a too-small memory limit and observe `OOMKilled`; (4) set a tight CPU limit and measure p99 vs no limit with a load test; (5) kill a pod during traffic and measure errors with and without graceful shutdown.

### Lab 3: Design the platform and cost model (paper)
For the Chapter 6 reference architecture at 10,000 RPS peak (stateless app tier, cache, primary + 2 replicas, queue + workers, CDN + object storage): choose compute models per component, the VPC/subnet layout across 3 AZs, node pool design (on-demand baseline, spot for workers), requests/limits and HPA settings, IaC layout (state split, modules, environments), DR strategy and RTO/RPO, and a monthly cost estimate (compute, storage, network by line item). Identify the top three cost levers and the top three failure-domain risks.

---

## 23.14 Interview perspective

- Translate boxes into **regions → AZs → subnets → services**, naming failure domains and N+1 capacity.
- State the compute choice with reasons (Kubernetes vs CaaS vs FaaS) and managed vs self-hosted for each stateful component.
- Explain **requests vs limits vs usage** and why over-requesting is the most common Kubernetes cost problem (25% real utilization).
- Describe **HPA/cluster autoscaling delays**, flapping controls, headroom, and spot economics with checkpointing.
- Mention **IaC, GitOps, drift, policy as code, and separate accounts**.
- Estimate network costs (**cross-AZ, NAT, egress**) and propose topology-aware routing, VPC endpoints, and CDN.
- Discuss multi-AZ by default, multi-region when justified, and why multi-cloud is rarely the best resilience investment.

---

## 23.15 Common misconceptions

1. **“Kubernetes makes us cloud-agnostic.”** Compute yes; data, IAM, networking, and managed services no.
2. **“Containers are lightweight VMs.”** They share the kernel and have weaker isolation.
3. **“More nodes = more reliability.”** Only with spread, headroom, and PDBs.
4. **“Requests are just hints.”** They are what the scheduler (and your bill) uses.
5. **“CPU limits protect neighbors.”** They also throttle you; requests protect neighbors.
6. **“Autoscaling will absorb any spike.”** Boot/warm-up delays remain (107 overloaded seconds in the lab).
7. **“Multi-AZ means highly available.”** Only if every dependency is.
8. **“Spot is unreliable, so avoid it.”** With checkpoints and diversification it saves ~67%.
9. **“Managed services are too expensive.”** Count engineer time and on-call.
10. **“Terraform makes drift impossible.”** Only if console changes are restricted and drift is detected.
11. **“Multi-cloud gives resilience.”** It often adds complexity without removing shared-fate failures.
12. **“One big cluster is simpler.”** Until its upgrade or control plane fails.

---

## 23.16 Chapter summary

- **Cloud basics:** regions and AZs are the failure domains; service scopes (zonal/regional/global) and quotas matter; shared responsibility; pricing models (reserved for baseline, spot for interruptible, on-demand for peaks); network egress is a major hidden cost.
- **Containers** = namespaces + cgroups + layered images on a shared kernel; memory limits cause OOMKills and CPU limits cause throttling; microVMs/gVisor for untrusted code.
- **Kubernetes** = declarative reconciliation: API server + etcd + scheduler + controllers + kubelets; scheduling is by **requests**; QoS classes; probes; rolling updates with PDBs and graceful shutdown; HPA/VPA/Cluster Autoscaler/KEDA.
- **Measured:** pods used 32% of requested CPU (cluster **25% truly utilized**, memory-bound); right-sizing requests cut nodes **74 → 54 (−27%)** whereas packing algorithms differed by ~3%; node size trades blast radius vs overhead, not cost. HPA with no damping made **250 scaling events in 2 h**; stabilization + tolerance reduced them to **34** for ~23% more pod-minutes, but spike overload (~107 s) needed headroom. Spot with checkpoints every 30–60 min saved **67%**; no checkpoints never finished; 24 h checkpoints cost more than on-demand. Random cross-AZ routing for one service pair cost **$6.9k/month** vs **$1.0k** zone-aware.
- **IaC/GitOps:** declarative code, remote locked state split by component, drift detection, plan review, policy as code, separate accounts, immutable infrastructure.
- **Networking:** VPC with public/private/isolated subnets per AZ, security groups, PrivateLink/endpoints, CIDR planning, NAT and egress costs.
- **Managed vs self-hosted:** default to managed for commodity services; weigh lock-in as a spectrum.
- **Resilience geography:** multi-AZ by default, multi-region when justified (static stability), multi-cloud rarely.

### Mental models
| Concept | Mental model |
|---|---|
| Region / AZ | A city and its separate power-grid districts |
| Container | A process in a one-room apartment sharing the building's foundation (kernel) |
| Requests | The seats you reserved; you pay for them whether or not you sit |
| Limits | The velvet rope that stops you from taking more than your share |
| Scheduler | A seating host fitting parties to tables by their reservations |
| HPA | A thermostat with delay: without damping it flaps |
| Spot | Standby airline seats: cheap, but you may be bumped, so keep your luggage (checkpoints) light |
| IaC | The architect's blueprint instead of verbal instructions to builders |
| Drift | The building no longer matches its blueprint |
| Cross-AZ traffic | Toll roads between districts |

---

## 23.17 Exercises

### A. Conceptual
1. Explain regions, AZs, and edge locations. Give two examples each of zonal, regional, and global services and why scope matters for availability.
2. State the shared responsibility model with three examples of customer failures.
3. Compare IaaS, CaaS, managed Kubernetes, PaaS, and FaaS on control, effort, and cost behavior.
4. What are namespaces and cgroups? Explain how a container differs from a VM, including isolation limits and when to use microVMs.
5. Explain why a JVM container gets OOMKilled with `-Xmx` equal to the memory limit. How would you size it?
6. Explain CPU throttling with CFS quotas and why p99 suffers. When would you omit CPU limits?
7. Describe Kubernetes components and what each does. What lives in etcd and why does it matter?
8. What is a reconciliation loop? Give an example with a Deployment.
9. Define requests, limits, and QoS classes; what gets evicted first under memory pressure?
10. Explain readiness, liveness, and startup probes and a harmful liveness-probe design.
11. How do PDBs, `maxSurge/maxUnavailable`, and graceful shutdown prevent outages during upgrades?
12. Explain the HPA formula, tolerance, and scale-down stabilization. Why does CPU HPA depend on requests?
13. What are the cluster autoscaler and Karpenter, and why can't they handle sharp spikes alone?
14. Why are spot instances unsuitable for etcd members or database primaries but good for workers?
15. Explain Young's rule for checkpoint intervals and why checkpointing too often or too rarely both hurt.
16. What are IaC state, plan, drift, and modules? Why remote locked state and split states?
17. Describe GitOps and how it differs from push-based CI deployment.
18. Design a three-tier VPC layout and explain what belongs in each subnet type.
19. Where do cross-AZ, NAT, and egress costs come from and how do you reduce each?
20. Compare managed and self-hosted databases in total cost of ownership.
21. Why is multi-region within one provider often better than multi-cloud for resilience?
22. What is static stability in cloud failover, and how does control-plane dependence break it?
23. List five FinOps practices.

### B. Calculations
24. A cluster has 40 nodes of 16 vCPU / 64 GiB. Pods request 560 vCPU and 2,100 GiB total; usage averages 190 vCPU and 1,200 GiB. Compute requested and actual utilization (CPU, memory). Which resource binds? If you right-size requests to 1.3× usage, how many nodes are needed (assume perfect packing)? Cost savings at $0.77/node-hour?
25. HPA: 12 pods at 85% average CPU (of requests), target 60%. Compute desired replicas. At 64% what happens given the 10% tolerance? With 3 recommendations in the last 5 minutes of 14, 11, 9 and a current of 14 pods, what does stabilization do on scale-down?
26. Spot: a 40-hour job, MTBF 8 h, checkpoint cost 90 s. Young's interval? Expected number of interruptions? Spot at 35% of on-demand: estimate savings if wall-clock increases by 8%.
27. Reserved vs on-demand: a 1-year reservation costs 62% of on-demand for the full year. Break-even utilization? A service runs 14 hours a day, 5 days a week: should you reserve?
28. Network: a service pair exchanges 80 MB/s continuously; 3 AZs; random routing; cross-AZ cost $0.01/GB each way; compute monthly cost; with 85% same-AZ routing; savings. NAT: 30 TB/month → cost at $0.045/GB.
29. CIDR: a VPC `10.0.0.0/16` with 3 AZs and 4 subnet tiers (public, app, pods, data). Propose sizes. A cluster of 400 nodes with 30 pods each using VPC-IP-per-pod: IPs required? Fits in a /19?
30. Node sizing: largest pod requests 14 vCPU / 48 GiB; typical pods 0.5 vCPU / 2 GiB; choose between 16/64 and 32/128 nodes for 600 pods total; compute nodes needed for each (ignore system overhead), and the percentage of capacity lost when one node dies.
31. Availability: three managed components each with a 99.95% SLA in series with your own stateless tier at 99.9% (multi-AZ, independent failures). Composite availability and monthly downtime. How would adding a second region active–passive (99% effective failover success) change it?
32. Image pull: 1.5 GB image, 800 Mbit/s effective node bandwidth, 25 nodes scaling out simultaneously from a single registry endpoint limited to 2 Gbit/s. Time to pull on each node vs total? Mitigations?

### C. Design
33. Design the infrastructure for a stateless API handling 20,000 RPS across 3 AZs: compute choice, node pools (on-demand baseline vs spot), autoscaling, requests/limits, load balancing, PDBs, and the headroom plan for AZ loss.
34. Design the IaC repository structure and pipeline for 3 environments × 2 regions with policy checks, drift detection, and break-glass procedures.
35. Design the VPC and connectivity for a platform with Kubernetes, managed databases, and an on-prem data center; include CIDR plan, endpoints, DNS, and egress control.
36. Design a cost-governance program (tags, budgets, showback, anomaly alerts, rightsizing cadence) and the metrics you would report monthly.
37. Design a migration from VMs to Kubernetes for 60 services without a big-bang cutover (order, tooling, rollback, risk controls).
38. Decide between self-hosted Kafka on Kubernetes and a managed Kafka service for a 40 MB/s workload and a 5-person platform team. Justify with cost and risk.

### D. Debugging
39. Pods are `OOMKilled` every few hours; heap usage looks fine. Diagnose and fix.
40. p99 latency doubled after a migration to containers although CPU usage is 30%. Hypotheses and checks.
41. The HPA scales between 10 and 40 pods every few minutes. Diagnose and stabilize.
42. A node drain during an upgrade took the service down. What was missing?
43. Terraform wants to destroy a database that was resized manually. What happened and how do you reconcile safely?
44. The cloud bill shows $9k/month for NAT gateway processing. How do you find the source and reduce it?

### E. Interview-style
45. “Design the infrastructure for a globally available web application.”
46. “How would you cut our Kubernetes bill by 30% without hurting reliability?”
47. “Kubernetes or serverless for our new service?”

### F. Teach-back
48. Explain requests vs limits vs actual usage to a finance colleague using hotel reservations.

---

## 23.18 Full answer key

**1.** Region: independent geographic area with multiple AZs; AZ: independent data-center group with its own power/network; edge: CDN/DNS presence. Zonal: VM, EBS volume; regional: load balancer, S3, managed DB with Multi-AZ; global: IAM, Route 53. A "multi-AZ" app is only as available as its least redundant dependency (e.g., a zonal NAT gateway).

**2.** Provider secures physical, hypervisor, managed-service internals; customer secures identities, configuration, data, network rules, OS/container patching. Failures: public S3 bucket, overly broad IAM role, unpatched VM/container image.

**3.** IaaS: most control/effort; CaaS: containers without servers; managed K8s: control plane managed, you run nodes/workloads; PaaS: deploy code; FaaS: functions with per-invocation billing. Cost: unit price falls with control and effort but total cost depends on utilization; FaaS cost inverts at high steady load.

**4.** Namespaces isolate what a process sees; cgroups limit what it uses. Containers share the host kernel (fast, dense, weaker isolation); VMs have separate kernels. Use microVMs/gVisor for untrusted or multi-tenant code.

**5.** The JVM uses memory beyond heap (metaspace, thread stacks, direct buffers, code cache) so total RSS exceeds `-Xmx`. Size heap to ~60–75% of the limit (`MaxRAMPercentage`), cap direct memory and threads, monitor RSS.

**6.** CFS grants `limit` CPU-time per 100 ms; bursty multi-threaded code exhausts its quota early in the period and is paused until the next, adding up to ~100 ms stalls. Omit or raise CPU limits for latency-sensitive services; keep requests for scheduling fairness.

**7.** API server (single entry, auth/authz), etcd (Raft-replicated state), scheduler (placement), controller manager (reconciliation), kubelet (runs pods/probes), container runtime, CNI/kube-proxy (networking). Losing etcd loses cluster state, so back it up and replicate.

**8.** Controllers compare desired and actual state and act. Deployment controller: desired 3 replicas, one pod dies → ReplicaSet creates another.

**9.** Requests: reserved amount used for scheduling; limits: cap; QoS: Guaranteed (requests=limits), Burstable, BestEffort. Under memory pressure BestEffort is evicted first, then Burstable above requests.

**10.** Startup: allow slow boots; readiness: gate traffic; liveness: restart if stuck. Harmful: liveness checking the database—if the DB blips, all pods restart together.

**11.** PDBs cap voluntary evictions (e.g., minAvailable 2), `maxSurge/maxUnavailable` pace rollouts, graceful shutdown (preStop, SIGTERM handling, termination grace) lets in-flight requests finish and the LB deregister.

**12.** `desired = ceil(current × metric/target)`; tolerance (default 10%) ignores small deviations; scale-down uses the highest recommendation in the stabilization window. CPU utilization is relative to *requests*, so wrong requests produce wrong scaling.

**13.** They add nodes only after pods are unschedulable, then nodes boot for minutes and pods warm up; they react, so sharp spikes outrun them. Use headroom, predictive/pre-scaling, queues.

**14.** Reclaim removes members/primaries abruptly, risking quorum loss or failovers; workers can be retried/rescheduled and tasks are idempotent.

**15.** Optimal interval ≈ sqrt(2 × checkpoint cost × MTBF). Too rare: lost work dominates; too frequent: checkpoint overhead dominates (lab: 24 h worse than on-demand; 5 min wasteful; 30–60 min ≈ 67% saving).

**16.** State maps code to real resources; plan = diff of desired vs state vs reality; drift = reality ≠ code; modules reuse standards. Remote locked state prevents concurrent corruption; splitting limits blast radius and lock contention.

**17.** GitOps: Git is the source of truth; an in-cluster controller pulls and reconciles (drift correction, audit trail, no cluster credentials in CI). Push-based CI applies changes with credentials from outside.

**18.** Public subnets (load balancers, NAT), private app subnets (services/nodes; outbound via NAT), isolated data subnets (databases, no internet route), per AZ.

**19.** Cross-AZ: chatty service pairs spread over zones → topology-aware routing; NAT: image pulls/API calls → VPC endpoints, local registries; egress: serving to the internet → CDN. 

**20.** Managed: higher unit price, minimal ops, built-in HA; self-hosted: cheaper compute at scale but requires on-call, expertise, upgrades, backups; include engineer-hours in TCO.

**21.** Multi-region on one provider uses consistent tooling and IAM and covers regional failures; multi-cloud adds incompatible services, double tooling, egress costs, and rarely-tested failover, and shared-fate (DNS/CA/people) remains.

**22.** The failover works without needing new control-plane actions (capacity already provisioned, DNS pre-configured). If failover requires launching instances or IAM calls in an impaired region, it can fail when needed.

**23.** Tagging/allocation, budgets and anomaly alerts, showback/chargeback, rightsizing and idle cleanup, commitment management (reserved/savings plans), unit-economics reporting, cost reviews in design.

**24.** Requested CPU util = 560/640 = **87.5%**; memory = 2,100/2,560 = **82%**; actual CPU = 190/640 = **29.7%**; actual memory = 1,200/2,560 = 46.9%. CPU binds. Right-size: CPU 1.3 × 190 = 247 vCPU → 247/16 = 15.4 nodes; memory 1.3 × 1,200 = 1,560 GiB → 1,560/64 = 24.4 nodes → **25 nodes** (memory binds after right-sizing). Savings: 15 nodes × $0.77 × 730 = **$8,430/month**.

**25.** 12 × 85/60 = 17.0 → **17 replicas**. At 64%: ratio 64/60 = 1.067, within 10% tolerance → no change. Scale-down: recommendations 14, 11, 9; the stabilization window takes the **maximum (14)**, so with 14 current pods nothing is removed until all recent recommendations fall.

**26.** Young: sqrt(2 × 0.025 h × 8 h) = sqrt(0.4) = 0.632 h ≈ **38 minutes**. Expected interruptions ≈ 43.2 h / 8 h ≈ **5.4**. Cost: 1.08 × 0.35 = 0.378 of on-demand → **≈ 62% savings**.

**27.** Break-even utilization = 62%. The service runs 14 × 5 / (24 × 7) = 70/168 = **41.7%** of hours, below 62%: **don't reserve** (or use on-demand with scheduled scaling/spot); reserve only the always-on baseline.

**28.** 80 MB/s × 2,592,000 s = 207,360 GB/month. Random routing: 2/3 cross → 138,240 GB × $0.02 = **$2,765**. 85% same-AZ: 15% cross → 31,104 GB × $0.02 = **$622** (saves **$2,143**). NAT: 30,000 GB × $0.045 = **$1,350**.

**29.** Example: public subnets /24 per AZ (251 IPs each), app subnets /22 per AZ (1,019 IPs), pods subnets /19 per AZ (8,187 IPs), data subnets /24 per AZ. 400 nodes × 30 pods = 12,000 pod IPs (+ node IPs) → exceeds a single /19 (8,192), but across 3 AZs three /19s give ~24,500: fits. If limited to one /19, it does not fit.

**30.** 16/64: pods total ~ 600 × 0.5 = 300 vCPU (plus a few 14-vCPU pods): 300/16 ≈ 19 nodes (memory 600 × 2 = 1,200 GiB/64 = 18.75) → ~20 nodes, one node loss = **5%** capacity. 32/128: ~10 nodes; one node loss = **10%** capacity. The large pod (14 vCPU) fits on 16-vCPU nodes only with almost nothing else on the node (fragmentation), so 32-vCPU nodes pack it better.

**31.** Series: 0.9995³ × 0.999 = 0.99850 × 0.999 = **0.99750** → 0.25% × 43,200 min = **108 min/month**. Adding an active–passive second region with 99% failover success: availability ≈ 1 − (1 − 0.9975)(1 − 0.99 × 0.9975) ≈ 1 − 0.0025 × 0.0125 ≈ **99.997%** in theory (realistically limited by shared control-plane and failover risk); the real constraint is failover correctness and correlated dependencies.

**32.** Per node alone: 1.5 GB × 8 / 0.8 Gbit/s = **15 s**. Registry bound: 25 nodes × 1.5 GB = 37.5 GB × 8 / 2 Gbit/s = **150 s** total; so the registry is the bottleneck (each node effectively gets 80 Mbit/s → 150 s). Mitigations: pre-pulled/baked images, regional registry mirrors/pull-through caches, smaller images/layers, P2P distribution (Dragonfly/Kraken), staggered rollouts.

**33 (sketch).** Container platform (managed K8s or Fargate) with ≥ 3 AZs; node pools: on-demand baseline sized for 60% load plus spot for burst (diversified); HPA on RPS/pod with 55% target and stabilization; requests from p95 usage; memory limit ≈ request; no CPU limit; PDB minAvailable 70%; topology spread; headroom pods; 1.5× capacity for AZ loss (3 AZs); ALB cross-zone; autoscaler with Karpenter.

**34 (sketch).** Repo per domain (network, platform, apps) with modules; environments as separate accounts and state; pipeline: fmt/validate → policy checks → plan comment → approvals → staged apply (dev→staging→prod region by region); nightly drift detection; break-glass role with time-limited credentials and alerts.

**35 (sketch).** Hub-and-spoke with Transit Gateway; non-overlapping CIDRs (e.g., 10.0/16 prod, 10.1/16 stage, on-prem 172.16/12); VPN/Direct Connect to on-prem; PrivateLink/endpoints for managed services; private hosted zones with forwarding to on-prem DNS; egress via inspection VPC with allowlists; subnets per tier and AZ.

**36 (sketch).** Mandatory tags enforced by policy; budgets per team with alerts; weekly anomaly review; showback dashboards; quarterly rightsizing and commitment reviews; unit costs (cost/request, cost/tenant); idle resource cleanup automation; cost in design reviews.

**37 (sketch).** Containerize with health/shutdown first; build platform (CI/CD, observability, security baselines); migrate low-risk stateless services first, parallel-run behind the same load balancer with weighted traffic; migrate stateful services last (or keep managed); rollback by shifting weights; track migration metrics; decommission VMs after bake time.

**38 (sketch).** 40 MB/s is modest; a 5-person team lacks capacity for 24×7 Kafka operations; managed Kafka premium (~$ few thousand/month) < cost of an on-call burden and incident risk; self-host only if cost or customization needs become decisive at much larger scale.

**39.** Off-heap/native memory, thread stacks, metaspace, direct buffers push RSS beyond the limit. Check container RSS vs heap, use NMT/Prometheus `container_memory_working_set_bytes`, lower heap percentage, raise limit, cap direct memory/threads.

**40.** CPU throttling due to limits, noisy neighbors, cross-AZ latency, cold caches/JIT after rollout, slow DNS or service discovery, overlay network overhead, connection pool changes. Check `container_cpu_cfs_throttled_*`, network latency, JVM warm-up, node pressure.

**41.** Noisy metric, no stabilization/tolerance, requests mismatched, slow readiness. Add scale-down stabilization, behavior limits, scale on RPS/queue lag, fix requests, speed readiness, add headroom.

**42.** No PodDisruptionBudget, insufficient replicas/topology spread, no graceful termination/preStop, or readiness not gating. Add PDB, spread across nodes/AZs, graceful shutdown, surge capacity.

**43.** Drift: manual change not in code. Reconcile by updating the code to match (import/refresh state), review plan, protect the database with `prevent_destroy`/deletion protection, and restrict manual changes.

**44.** Use flow logs / VPC traffic analysis / cost allocation tags to find sources (image pulls, S3/DynamoDB via NAT, telemetry exports); add VPC endpoints (gateway endpoints are free for S3/DynamoDB), regional registry mirrors, route internal traffic privately, cache downloads.

**45 (sketch).** Multi-region active–active: global DNS/anycast + CDN, regional stacks (3 AZs each) with stateless tier on K8s/CaaS, regional data stores with replication (Ch 9/15), static stability, IaC per region, observability, security baselines.

**46 (sketch).** Measure utilization; right-size requests (VPA), remove idle environments, adopt savings plans for baseline, spot for batch/workers, ARM instances, topology-aware routing and VPC endpoints to cut network cost, tune autoscaling, delete orphaned volumes/IPs, set budgets and showback.

**47 (sketch).** Serverless for spiky, event-driven, low-ops; Kubernetes/CaaS for steady traffic, long-lived connections, custom runtimes, many teams; consider cold start, limits, cost curve, portability, team skills, existing platform.

**48.** Requests are the rooms you reserved: the hotel charges for them even if you never use them. Limits are the maximum rooms you can take. Usage is how many rooms you actually occupy. If you reserve a floor "to be safe" but use two rooms, you pay for the floor, and the hotel turns away other guests (more nodes).

---

## 23.19 Flashcards

| Front | Back |
|---|---|
| Region / AZ | Geographic area / independent failure domain within it |
| Shared responsibility | Provider: the cloud; you: what you put in it |
| Pricing mix | Reserved for baseline, spot for interruptible, on-demand for peaks |
| Container | Process + namespaces (see) + cgroups (use) on shared kernel |
| OOMKill | Memory limit exceeded; heap < limit (RSS includes off-heap) |
| CPU throttling | CFS quota per 100 ms; hurts p99 |
| K8s control plane | API server, etcd, scheduler, controller manager |
| Scheduler uses | Requests, not usage |
| QoS | Guaranteed / Burstable / BestEffort |
| PDB | Limits voluntary disruptions during drains/upgrades |
| HPA formula | ceil(current × metric / target); tolerance 10%; scale-down stabilization 5 min |
| Lab: usage | Pods used 32% of requested CPU → 25% cluster utilization |
| Lab: right-sizing | 74 → 54 nodes (−27%); packing algorithm differences ~3% |
| Lab: flapping | 250 → 34 scaling events with damping; +23% pod-minutes |
| Spot checkpointing | Interval ≈ √(2·cost·MTBF); 30–60 min → 67% saving; none → never finishes |
| IaC | Declarative code, remote locked state, plan, drift detection |
| GitOps | Git as truth; in-cluster reconciler |
| VPC tiers | Public / private app / isolated data per AZ |
| Cross-AZ lab | $6,912 random vs $1,037 zone-aware per service pair per month |
| Managed vs self-host | Default managed; include engineer-hours |
| Multi-cloud | Rarely worth it; multi-region first |
| Static stability | Failover without control-plane actions |

## 23.20 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Region / AZ | Area / zone | Geographic area / isolated data-center group | Failure domains | us-east-1a |
| IaaS / PaaS / CaaS / FaaS | Service levels | Rent VMs / platform / containers / functions | Trade control for effort | EC2, Lambda |
| Hypervisor | VM host | Software running guest OSes | Isolation | KVM, Nitro |
| Container | Isolated process | Namespaces + cgroups + image | Dense, fast deploys | Docker |
| Image / registry | Packaged app / store | Layered OCI image / repository | Distribution | ECR |
| cgroup / namespace | Limit / isolate | Kernel features for resources / visibility | Container basis | `memory.max` |
| OOMKilled | Out of memory kill | Process exceeded memory limit | Failure mode | Exit 137 |
| Kubernetes | Container orchestrator | Declarative control plane + nodes | Run at scale | EKS |
| Pod | Smallest unit | Co-scheduled containers sharing network | Deployment unit | `checkout-7f9` |
| Deployment / StatefulSet | Stateless / stateful workload | Replica management objects | Rollouts | — |
| Request / limit | Reservation / cap | Scheduler resource claim / cgroup cap | Scheduling, isolation | `cpu: 500m` |
| QoS class | Eviction priority | Guaranteed/Burstable/BestEffort | Pressure handling | — |
| PDB | Disruption budget | Min available pods during voluntary disruption | Safe upgrades | `minAvailable: 2` |
| HPA / VPA | Horizontal / vertical autoscaler | Replica / request autoscaling | Match demand | CPU 60% |
| Cluster autoscaler / Karpenter | Node scaler | Adds/removes nodes for pending pods | Capacity | — |
| Spot instance | Interruptible VM | Spare capacity reclaimable at short notice | Cost | 2-minute notice |
| Savings plan / reservation | Commitment discount | 1–3 year usage commitment | Baseline cost | 40% off |
| IaC | Infra as code | Declarative infrastructure definitions | Repeatability | Terraform |
| Drift | Reality ≠ code | Out-of-band changes | Governance | — |
| GitOps | Git-driven ops | Controller reconciles from Git | Auditability | Argo CD |
| VPC / subnet | Private network / segment | Isolated virtual network | Segmentation | 10.0.0.0/16 |
| NAT gateway | Outbound gateway | Translates private egress to internet | Egress | — |
| PrivateLink / endpoint | Private service access | Private connectivity to services | Security, cost | S3 gateway endpoint |
| Egress | Data out | Outbound traffic charges | Cost driver | $0.09/GB |
| FinOps | Cloud financial ops | Cost allocation, optimization practices | Control spend | Showback |
| Static stability | Works without reacting | Pre-provisioned failover capacity | Resilience | — |

---

**Batch 6 complete.** Part IV is finished (Ch 21 observability, Ch 22 security, Ch 23 cloud/containers/infrastructure).

**Pausing here, as you requested.** Part V (Ch 24–30: the design method, the four problem books, deconstructing real systems, and the capstone) and the appendix are still ahead.
