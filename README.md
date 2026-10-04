# Kubernetes Incident Copilot

> AI-assisted, evidence-grounded Kubernetes incident triage using Prometheus, Alertmanager, Python, and a local Ollama/Mistral model.

## Overview

Kubernetes Incident Copilot is a personal SRE/DevOps project that demonstrates an event-driven incident-triage workflow on a local Kind cluster.

Instead of asking an LLM to monitor Kubernetes directly, the project keeps detection deterministic:

1. **kube-state-metrics** exposes Kubernetes workload state.
2. **Prometheus** evaluates an alert rule for abnormal container restart growth.
3. **Alertmanager** routes the matched alert to a Python/Flask webhook.
4. **Python** collects read-only Kubernetes evidence for the affected pod.
5. **Ollama** serves a local **Mistral** model.
6. **Mistral** interprets the supplied evidence and returns a diagnosis, confidence level, and suggested read-only checks.
7. A **human SRE** remains responsible for remediation.

The key design principle is simple:

> **Prometheus detects. Alertmanager triggers. Python investigates. Kubernetes provides evidence. Ollama serves Mistral. Mistral interprets the evidence. The SRE decides.**

## Architecture

```text
Kind Kubernetes
      |
      | Pod restart / CrashLoopBackOff
      v
kube-state-metrics
      |
      v
PrometheusRule --> Prometheus
                      |
                      v
                 Alertmanager
                      |
                      | HTTP POST /webhook
                      v
                Python / Flask
                 /          \
                /            \
               v              v
      Kubernetes API       Ollama API
               |              |
   pod state + events          v
   + previous logs          Mistral
               \              /
                \            /
                 v          v
             Evidence-grounded
             incident diagnosis
                    |
                    v
                 Human SRE
```

## Tech Stack

- Kubernetes / Kind
- Prometheus
- Alertmanager
- kube-state-metrics
- Python
- Flask
- Kubernetes Python client
- Ollama
- Mistral
- Argo CD / GitOps concepts

## Incident Scenario

The lab simulates a controlled application failure by making the workload container exit with a non-zero status. Kubernetes repeatedly restarts the container, eventually presenting a `CrashLoopBackOff` condition.

A representative game-day patch is:

```bash
kubectl patch deployment wealthops -n wealthops --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/command","value":["/bin/sh","-c","echo Intentional-SRE-GameDay-Crash; exit 1"]}]'
```

This causes restart growth that can be observed through:

```text
kube_pod_container_status_restarts_total
```

## Prometheus Alert Rule

The custom rule detects repeated restarts in the application namespace.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: wealthops-crashloop
  namespace: monitoring
spec:
  groups:
    - name: wealthops.rules
      rules:
        - alert: WealthOpsCrashLoopBackOff
          expr: |
            increase(
              kube_pod_container_status_restarts_total{namespace="wealthops"}[5m]
            ) > 2
          for: 1m
          labels:
            severity: critical
          annotations:
            summary: "WealthOps pod is restarting repeatedly"
            description: "Pod {{ $labels.pod }} in {{ $labels.namespace }} has restarted more than twice in 5 minutes."
```

The LLM is **not** responsible for deciding when this alert fires. Detection remains metric- and rule-based.

## Alertmanager Routing

Only the project-specific alert is routed to the Incident Copilot webhook. Other alerts can continue to a different/default receiver.

```yaml
route:
  receiver: "null"
  group_by:
    - alertname
    - namespace

  routes:
    - receiver: incident-copilot
      matchers:
        - alertname = "WealthOpsCrashLoopBackOff"

receivers:
  - name: "null"

  - name: incident-copilot
    webhook_configs:
      - url: http://host.docker.internal:5000/webhook
        send_resolved: false
```

In this local setup, Alertmanager runs inside Kind/Docker while Flask runs on the host, so `host.docker.internal` provides the host-side endpoint used by the lab.

## Python Webhook Flow

The Flask service is started once:

```bash
python incident_webhook.py
```

The webhook waits for Alertmanager:

```python
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(force=True)

    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        namespace = labels.get("namespace", "unknown")
        pod = labels.get("pod", "unknown")
```

The alert labels identify the workload that needs investigation. The Copilot therefore does not require a person to manually type the affected pod name for each incident.

## Evidence Collection

The collector uses read-only Kubernetes API calls.

### Pod state

```python
pod = core_v1.read_namespaced_pod(pod_name, namespace)
```

### Kubernetes events

```python
events = core_v1.list_namespaced_event(namespace)
```

### Previous container logs

```python
logs = core_v1.read_namespaced_pod_log(
    pod_name,
    namespace,
    tail_lines=50,
    previous=True
)
```

For the controlled failure, the previous logs can expose the game-day marker:

```text
Intentional-SRE-GameDay-Crash
```

This evidence is assembled before the model is called.

## Grounded LLM Prompt

The prompt is designed for incident triage rather than unrestricted cluster automation. It instructs the model to:

- reason only from supplied evidence;
- state when evidence is insufficient;
- avoid inventing logs, metrics, or causes;
- provide read-only investigation suggestions;
- return a clear diagnosis and confidence level;
- avoid performing cluster remediation.

This keeps the LLM in an **advisory** role.

## Ollama / Mistral Integration

Python calls the local Ollama Generate API with Mistral:

```python
payload = {
    "model": "mistral:latest",
    "prompt": prompt,
    "stream": False,
    "options": {
        "temperature": 0.1
    }
}
```

Endpoint:

```text
POST http://localhost:11434/api/generate
```

The result is returned to the Python service and displayed as an evidence-based incident analysis.

Example output structure:

```text
DIAGNOSIS
<evidence-based explanation>

CONFIDENCE
High / Medium / Low

SUGGESTED READ-ONLY KUBECTL CHECKS
- ...
- ...
```

## End-to-End Processing Stages

A successful incident demonstrates these six stages:

```text
STAGE 1 - ALERTMANAGER WEBHOOK RECEIVED
STAGE 2 - COLLECTING KUBERNETES EVIDENCE
STAGE 3 - BUILDING LLM PROMPT
STAGE 4 - CALLING OLLAMA / MISTRAL
STAGE 5 - AI RESPONSE RECEIVED
STAGE 6 - WEBHOOK PROCESSING COMPLETE
```

The final webhook request should complete successfully with an HTTP `200` response.

## Troubleshooting Lesson

One of the most useful failures encountered while building the project was in the Alertmanager-to-Flask integration.

Prometheus was firing the expected alert and Alertmanager could see it, but Flask was not receiving the webhook. The issue was isolated by validating each integration hop independently:

```text
Kubernetes CrashLoop             OK
kube-state-metrics               OK
Prometheus rule                  OK
Alertmanager receives alert      OK
Manual POST -> Flask             OK
Flask -> Kubernetes API          OK
Flask -> Ollama/Mistral          OK
Alertmanager -> Flask            FAILED
```

The generated Alertmanager runtime configuration was then compared with the intended configuration. Operator logs indicated that the expected Secret configuration key was missing. The Secret used `alertmanager.yml`, while the expected key was `alertmanager.yaml`.

After correcting the Secret key and validating the generated runtime configuration, Alertmanager routed the custom alert to the Flask webhook successfully.

### Lesson learned

For Kubernetes Operators, validating only Helm values or desired YAML is not sufficient. A useful troubleshooting pattern is:

```text
Desired configuration
       -> Kubernetes resource / Secret
       -> Operator reconciliation logs
       -> Generated runtime configuration
       -> Actual application behavior
```

## Recovery

After the game-day evidence is captured, the workload can be restored to its previous deployment revision:

```bash
kubectl rollout undo deployment/wealthops -n wealthops
kubectl rollout status deployment/wealthops -n wealthops
kubectl get pods -n wealthops
```

Recovery validation should also include the application-facing path rather than relying only on a `Running` pod status.

## Safety and Responsible AI Design

This project intentionally separates **analysis** from **remediation**.

- Prometheus provides deterministic detection.
- Kubernetes APIs provide runtime evidence.
- The LLM receives bounded evidence rather than unrestricted cluster context.
- The AI path is read-only.
- Recommendations are advisory.
- Human review remains part of the incident process.

A low temperature is used for conservative generation, but it is **not** treated as a guarantee against hallucination. Evidence grounding, restricted permissions, and human review are the primary controls.

## Running the Demo

A typical demo flow is:

1. Start the local Kind cluster and confirm the application is healthy.
2. Confirm Prometheus and Alertmanager are running.
3. Start Ollama and make the Mistral model available locally.
4. Start the Flask webhook service.
5. Inject the controlled container failure.
6. Watch Kubernetes restart the workload.
7. Confirm the Prometheus alert becomes `FIRING`.
8. Confirm Alertmanager selects the `incident-copilot` receiver.
9. Observe the six Flask processing stages.
10. Review the evidence-grounded diagnosis.
11. Restore the workload and validate recovery.

> **Note:** Commands and object names in this repository reflect a local learning environment. Review namespaces, image names, networking, RBAC, and configuration before adapting the project to another environment.

## Suggested Repository Structure

```text
kubernetes-incident-copilot/
├── README.md
├── .gitignore
├── requirements.txt
├── app/
│   └── incident_webhook.py
├── kubernetes/
│   ├── prometheus-rule.yaml
│   └── alertmanager-config.yaml
├── docs/
│   └── architecture.md
└── screenshots/
    ├── prometheus-alert-firing.png
    ├── alertmanager-routing.png
    └── copilot-diagnosis.png
```

Keep real credentials, tokens, kubeconfig files, private certificates, internal URLs, proprietary application artifacts, and secrets out of the public repository.

## Future Enhancements

Possible next iterations:

- Add structured JSON output from the LLM.
- Add automated evidence redaction before inference.
- Add more incident scenarios such as image pull failures, OOM kills, probe failures, and service/network issues.
- Store incident evidence and AI findings for later review.
- Add a UI or chat interface for incident summaries.
- Add unit/integration tests for alert payload parsing and evidence collection.
- Add CI validation for Python and Kubernetes manifests.
- Introduce an explicit approval workflow for any future remediation capability.

## Project Purpose

This is a personal learning/portfolio project focused on practical SRE, Kubernetes troubleshooting, observability, Python automation, and evidence-grounded AI integration. It is not intended to replace production incident-management controls or human operational judgment.
