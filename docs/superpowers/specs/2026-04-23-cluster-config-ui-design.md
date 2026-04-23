# Cluster Config UI — Design

**Date:** 2026-04-23
**Status:** Approved for implementation planning

## 1. Goal

Expose a subset of the HPA and Deployment configuration through the dashboard UI so the user can tune autoscaling behavior and per-pod CPU budgets live, without editing YAML files or re-running `./scripts/deploy.sh`. Editing is gated on the load generator being paused.

This adds a second dimension of control to the demo: today the user can only tune the load side (RPS, target URL). After this change the user can also tune the capacity side (how many pods, when to scale, how much CPU each pod can reserve and burn).

## 2. Scope

### 2.1 Parameters exposed

Six parameters, validated both client- and server-side:

| # | Parameter | Type | Range | Default (from YAML) | K8s target |
|---|-----------|------|-------|--------------------|-----------|
| 1 | `hpa.minReplicas` | int | 1–10 | 1 | HPA |
| 2 | `hpa.maxReplicas` | int | 1–20 | 10 | HPA |
| 3 | `hpa.targetCPUUtilization` | int (%) | 10–100 | 50 | HPA |
| 4 | `hpa.targetMemoryUtilization` | int (%) | 10–100 | 70 | HPA |
| 5 | `deployment.requests.cpu` | int (millicores) | 50–1000 | 100 | Deployment |
| 6 | `deployment.limits.cpu` | int (millicores) | 50–2000 | 500 | Deployment |

### 2.2 Cross-field rules

- `minReplicas ≤ maxReplicas`
- `requests.cpu ≤ limits.cpu`
- `maxReplicas > 10` is allowed but surfaces a non-blocking warning referencing `architecture-spec.md §5.5.5` (cluster capacity).

### 2.3 Out of scope (YAGNI)

- Named config presets / persistence of custom configs.
- Undo / change history.
- HPA `behavior.scaleUp` / `behavior.scaleDown` policy editing (stabilization windows, pod/percent rates).
- Memory request/limit editing.
- Editing the target URL of the load generator (already exists).

## 3. Gating

Editing is allowed **only when the load generator is paused** (`load_generator.is_running === false`).

- **UX (frontend):** while running, all inputs + Apply + Reset render `disabled` with a tooltip "Pause the load generator first", and a warning is shown in the panel. The panel itself remains expandable/readable.
- **Safety (backend):** `apply_cluster_config` and `reset_cluster_config` handlers reject with `status: "validation_error"` and `error: "load generator is running"` when `is_running` is true. The frontend gating is UX-only; this server-side guard is what actually prevents concurrent edits during load.

Rationale: avoids observing HPA mid-scaling while mutating its target, which would make the dashboard behavior confusing and the demo value unclear.

## 4. Apply Mechanism

Changes are applied via **direct K8s API patches**, not by rewriting YAML files.

- HPA: `AutoscalingV2Api.patch_namespaced_horizontal_pod_autoscaler` against `spec.minReplicas`, `spec.maxReplicas`, and the CPU/memory entries of `spec.metrics[*].resource.target.averageUtilization`.
- Deployment: `AppsV1Api.patch_namespaced_deployment` against `spec.template.spec.containers[0].resources.requests.cpu` and `.limits.cpu`.

The files in `k8s/` are **not rewritten**. They serve as the source of truth for the "initial / default" state of the cluster, read once by `get_defaults()`. Live cluster state may diverge from the YAML files until either a `reset` is triggered or `./scripts/deploy.sh` is re-run (which re-applies the YAMLs and wipes any custom config — documented behavior).

A **Reset to defaults** button re-reads `k8s/hpa.yaml` and `k8s/deployment.yaml` and applies those values via the same patch path.

### 4.1 Rolling restart on resource changes

Patching `spec.template.spec.containers[0].resources` triggers a rolling restart of the deployment (K8s default behavior on pod template changes). Patching the HPA does not restart pods.

After a patch that includes resource changes, `apply()` polls `AppsV1Api.read_namespaced_deployment_status` until `status.updatedReplicas == spec.replicas` and `status.observedGeneration >= metadata.generation`, with a 60-second timeout. The UI reflects this as an "Applying — pods restarting..." state.

### 4.2 Monitor sync

When `deployment.requests.cpu` changes, `K8sMonitor.cpu_request_millicores` must be updated in the same process, because it is the denominator of the per-pod `cpu_percent` reported in the UI. This is an in-memory update; no restart of the dashboard process is needed.

## 5. Backend Architecture

### 5.1 New module: `dashboard/cluster_config.py`

```python
@dataclass
class ClusterConfig:
    min_replicas: int
    max_replicas: int
    target_cpu_utilization: int
    target_memory_utilization: int
    cpu_request_millicores: int
    cpu_limit_millicores: int

@dataclass
class ApplyResult:
    status: Literal["ok", "validation_error", "k8s_error", "rollout_timeout"]
    error: str | None
    restart_triggered: bool

class ClusterConfigManager:
    def __init__(
        self,
        namespace: str,
        deployment_name: str,
        hpa_name: str,
        k8s_manifests_dir: Path,
        k8s_monitor: K8sMonitor,
    ): ...

    async def get_current(self) -> ClusterConfig: ...
        # Read live state: HPA (minReplicas, maxReplicas, metrics targets) +
        # Deployment (resources.requests/limits.cpu).

    async def get_defaults(self) -> ClusterConfig: ...
        # Parse k8s/hpa.yaml + k8s/deployment.yaml using PyYAML.

    async def apply(self, config: ClusterConfig) -> ApplyResult: ...
        # 1. Validate ranges + cross-field rules.
        # 2. Patch HPA.
        # 3. Patch Deployment (resources block).
        # 4. If cpu_request changed → k8s_monitor.cpu_request_millicores = new value.
        # 5. If resources changed → await rollout completion (timeout 60s).
        # 6. Return ApplyResult.
```

All blocking K8s client calls run via `asyncio.to_thread` (same pattern as `K8sMonitor.get_metrics`).

### 5.2 Integration in `dashboard/app.py`

- Instantiate `ClusterConfigManager` alongside `k8s_monitor`, passing it a reference to the monitor so it can mutate `cpu_request_millicores` in place.
- Add three WebSocket action handlers (`get_cluster_config`, `apply_cluster_config`, `reset_cluster_config`).
- On each successful apply/reset, broadcast a fresh `cluster_config` message to all connected clients (not just the requester) so multiple open tabs stay in sync.

### 5.3 New dependency

`PyYAML` — parses `k8s/hpa.yaml` and `k8s/deployment.yaml` in `get_defaults()`. Alternative considered: hardcoding defaults in Python. Rejected because it silently desyncs from the YAML files, which are the source of truth in the repo.

### 5.4 K8sMonitor change

`K8sMonitor.cpu_request_millicores` is already a public attribute (`self._cpu_request_m` internal; exposed via constructor). To keep the change minimal, rename the internal field to `self.cpu_request_millicores` (public), making it writable from `ClusterConfigManager.apply()` without a setter method.

## 6. WebSocket Protocol Additions

### 6.1 Client → Server

| Action | Payload | Effect |
|--------|---------|--------|
| `get_cluster_config` | `{"action": "get_cluster_config"}` | Reads current + defaults, server broadcasts `cluster_config` |
| `apply_cluster_config` | `{"action": "apply_cluster_config", "value": {<ClusterConfig fields>}}` | Validates, patches, waits for rollout. Server broadcasts `cluster_config_result` then updated `cluster_config` |
| `reset_cluster_config` | `{"action": "reset_cluster_config"}` | Equivalent to `apply_cluster_config` with `get_defaults()` values |

### 6.2 Server → Client

**`cluster_config`** — sent on connect (via the client's `get_cluster_config` request) and after each successful apply/reset:

```json
{
  "type": "cluster_config",
  "current": {
    "min_replicas": 1, "max_replicas": 10,
    "target_cpu_utilization": 50, "target_memory_utilization": 70,
    "cpu_request_millicores": 100, "cpu_limit_millicores": 500
  },
  "defaults": {
    "min_replicas": 1, "max_replicas": 10,
    "target_cpu_utilization": 50, "target_memory_utilization": 70,
    "cpu_request_millicores": 100, "cpu_limit_millicores": 500
  }
}
```

**`cluster_config_result`** — sent as the response to `apply_cluster_config` / `reset_cluster_config`:

```json
{
  "type": "cluster_config_result",
  "status": "ok" | "validation_error" | "k8s_error" | "rollout_timeout",
  "error": null | "maxReplicas must be >= minReplicas",
  "restart_triggered": true
}
```

### 6.3 Existing `metrics` message

Unchanged. `is_running` continues to live there and is what the frontend reads to gate the config panel.

## 7. Frontend Architecture

### 7.1 UI layout

New section inserted into `dashboard/static/index.html` between `arch-controls` and `arch-container`:

```
┌─ arch-controls (existing) ───────────────────────────────────┐
│ [Start]  RPS [===●===] 10   Target [http://...]              │
├─ cluster-config (new) ───────────────────────────────────────┤
│ ▸ Cluster config                           [● Synced]        │  ← collapsed
└──────────────────────────────────────────────────────────────┘

Expanded:
┌──────────────────────────────────────────────────────────────┐
│ ▾ Cluster config                           [● Synced]        │
│                                                              │
│  HPA                                                         │
│  min replicas  [1──●──] 1       max replicas [────●──] 10    │
│  target CPU %  [──●──] 50       target mem % [────●─] 70     │
│                                                              │
│  Deployment                                                  │
│  cpu request   [●────] 100m     cpu limit    [──●──] 500m    │
│                                                              │
│  ⚠ Pause the load generator to edit these values.            │  ← only if is_running
│                                                              │
│          [Reset to defaults]           [Apply]               │
└──────────────────────────────────────────────────────────────┘
```

Panel collapsed state persisted to `localStorage.clusterConfigExpanded`. Default: collapsed.

### 7.2 Badge states

Visible in the panel header, regardless of collapsed/expanded:

- `● Synced` (green) — `draft` equals `current`.
- `● Modified` (yellow) — `draft` differs from `current`.
- `● Applying...` (blue, pulsing) — patch or rollout in flight.
- `● Error` (red) — last apply failed; hover shows the server error message.

### 7.3 Frontend state

```js
let clusterConfig = {
    current: null,   // last-known live cluster state
    defaults: null,  // from YAML
    draft: null,     // what the user is editing
    state: 'synced', // 'synced' | 'modified' | 'applying' | 'error'
    error: null,     // last server error message
};
```

### 7.4 Interaction flow

1. **On WS open** → send `get_cluster_config`. Hydrate `draft = {...current}` on reply.
2. **On input change** → update `draft`, recompute `state` from `draft` vs `current`, re-validate, toggle Apply enablement.
3. **Click Apply** → `state = 'applying'`, disable all controls, send `apply_cluster_config`.
4. **Receive `cluster_config_result`**:
   - `ok` → wait for the follow-up `cluster_config` broadcast → `state = 'synced'`.
   - `validation_error` / `k8s_error` → `state = 'error'`, show message, keep `draft` so user can correct.
   - `rollout_timeout` → `state = 'error'`, message "Rollout is taking longer than expected; check pod status".
5. **Click Reset** → `state = 'applying'`, send `reset_cluster_config`.

### 7.5 Gating

`updateConfigPanelGating(isRunning)` is called inside `updateDashboard` (which already runs on every `metrics` message):

- If `isRunning === true` → add `disabled` to all inputs, Apply, Reset; show "Pause the load generator…" warning.
- Else → enable inputs; Apply enabled only if `state === 'modified'` and validation passes; Reset always enabled when not in `applying`.

### 7.6 Client-side validation

A pure helper `validateConfig(draft) → {errors: {field: message}}` runs on every change. Errors render inline (red text below the offending input). The server-side `apply()` re-validates — client validation is UX, server validation is correctness.

## 8. Error Handling

| Scenario | Detection | User-facing response |
|----------|-----------|---------------------|
| Value out of range | Frontend + server `apply` | Inline red message on input; Apply disabled |
| `minReplicas > maxReplicas` or `request > limit` | Both | Inline message on the conflicting pair |
| K8s API rejects patch (e.g. HPA missing) | `ApiException` in `apply` | `k8s_error` status, red badge, server error in tooltip |
| Rollout exceeds 60s | Polling timeout | `rollout_timeout` status, red badge, message suggests `kubectl get pods` |
| User attempts Apply while load running (UI bug) | Server guard | `validation_error`, message "load generator is running" |
| WS lost during `applying` | Existing auto-reconnect | On reconnect, `get_cluster_config` refetches; state reflects whatever the server confirmed |

## 9. Testing

Not blocking for v1 delivery, but recommended:

- **Unit:** `ClusterConfigManager.get_defaults()` against a YAML fixture; `validateConfig(draft)` covering range edges, `min==max`, `request==limit`, out-of-range values.
- **Integration:** `apply()` against a live k3d cluster — verify the patch lands and `K8sMonitor.cpu_request_millicores` updates in memory.
- **Manual smoke** (documented in PR description): pause → raise `maxReplicas` to 15 → Apply → ramp load to high RPS → observe pods scaling beyond 10.

## 10. File Map

```
dashboard/
├── cluster_config.py          [NEW]  ClusterConfigManager, ClusterConfig, ApplyResult
├── app.py                     [MOD]  Instantiate manager, 3 WS handlers, broadcast cluster_config
├── requirements.txt           [MOD]  +PyYAML
├── k8s_monitor.py             [MOD]  Rename internal _cpu_request_m to public cpu_request_millicores
└── static/
    ├── index.html             [MOD]  Panel HTML + state + validation + handlers
    └── style.css              [MOD]  Collapsible panel styles, badge states

docs/
└── architecture-spec.md       [MOD]  §3.2.5 (ClusterConfigManager), §4 (WS protocol additions)
```

## 11. Design Decisions

| # | Decision | Chosen | Alternatives | Why |
|---|----------|--------|--------------|-----|
| 1 | Parameter scope | HPA min/max/CPU/memory targets + Deployment CPU request/limit | A (fewer — no memory, no limit), C (+ HPA behavior windows) | Sweet spot: covers request/limit relationship (central to spec §5.5) without overwhelming the UI with rarely-adjusted behavior windows |
| 2 | Gating mechanism | Disable controls while running | Confirm dialog on Apply, auto-pause on Apply | Clearest about system state; avoids edit-during-scaling confusion; server-side guard backs it up |
| 3 | Apply mechanism | Direct K8s API patch + Reset-from-YAML | Rewrite YAML + kubectl apply, patch only | Dashboard already has K8s client; avoids filesystem writes from a backend; Reset gives an escape hatch |
| 4 | UI placement | Collapsible panel under `arch-controls` | Modal, fixed side column | Respects current layout, keeps live view visible while editing, collapse/expand is a natural secondary control affordance |
| 5 | Defaults source | Parse YAML with PyYAML | Hardcode in Python | YAML files stay the source of truth; avoids silent desync |
| 6 | Monitor update | Make `cpu_request_millicores` public, mutate in place | Setter method, pub/sub | Both components run in the same process; a direct attribute write is the simplest correct solution |
