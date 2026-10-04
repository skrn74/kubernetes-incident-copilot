import json
import urllib.request
from datetime import datetime, timezone

from flask import Flask, request
from kubernetes import client, config

app = Flask(__name__)

# Uses your existing local kubeconfig (same context as kubectl).
config.load_kube_config()

core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "mistral:latest"


def get_pod_evidence(namespace, pod_name):
    """
    Read-only evidence collection. No create/patch/delete calls here.
    """
    evidence = {}

    try:
        pod = core_v1.read_namespaced_pod(pod_name, namespace)
        evidence["pod_status"] = pod.status.phase
        evidence["container_statuses"] = [
            {
                "name": c.name,
                "restart_count": c.restart_count,
                "state": str(c.state),
            }
            for c in (pod.status.container_statuses or [])
        ]
    except Exception as error:
        evidence["pod_status_error"] = str(error)

    try:
        events = core_v1.list_namespaced_event(namespace)
        evidence["events"] = [
            f"{e.last_timestamp} {e.reason}: {e.message}"
            for e in events.items
            if pod_name in (e.involved_object.name or "")
        ][-10:]
    except Exception as error:
        evidence["events_error"] = str(error)

    try:
        logs = core_v1.read_namespaced_pod_log(
            pod_name, namespace, tail_lines=50, previous=True
        )
        evidence["previous_logs"] = logs
    except Exception:
        try:
            logs = core_v1.read_namespaced_pod_log(
                pod_name, namespace, tail_lines=50
            )
            evidence["current_logs"] = logs
        except Exception as error:
            evidence["logs_error"] = str(error)

    return evidence


def build_prompt(alert_labels, alert_annotations, evidence):
    return f"""
You are a Kubernetes Site Reliability Engineer performing read-only triage.

Rules:
1. Use only the evidence below. Do not invent causes, metrics, or logs.
2. If evidence is insufficient, say "Unknown from available evidence."
3. Do NOT recommend or imply running delete, patch, scale, or rollout commands.
4. Suggest only further READ-ONLY kubectl checks an SRE could run next.
5. Keep it short enough for a Slack/Teams incident channel.

Alert labels:
{json.dumps(alert_labels, indent=2)}

Alert annotations:
{json.dumps(alert_annotations, indent=2)}

Collected evidence:
{json.dumps(evidence, indent=2)}

Respond using exactly these headings:
DIAGNOSIS
CONFIDENCE (High/Medium/Low)
SUGGESTED READ-ONLY KUBECTL CHECKS
"""


def ask_ollama(prompt):
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))["response"].strip()


@app.route("/webhook", methods=["POST"])
def webhook():

    print("\n" + "=" * 70)
    print("STAGE 1 - ALERTMANAGER WEBHOOK RECEIVED")
    print("=" * 70)

    payload = request.get_json(force=True)

    print(json.dumps(payload, indent=2))

    results = []

    for alert in payload.get("alerts", []):

        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})

        namespace = labels.get("namespace", "unknown")
        pod = labels.get("pod", "unknown")

        print("\n" + "=" * 70)
        print("STAGE 2 - COLLECTING KUBERNETES EVIDENCE")
        print("=" * 70)

        print(f"Alert     : {labels.get('alertname')}")
        print(f"Namespace : {namespace}")
        print(f"Pod       : {pod}")

        evidence = get_pod_evidence(
            namespace,
            pod
        )

        print("\nCollected evidence:")
        print(json.dumps(evidence, indent=2))

        print("\n" + "=" * 70)
        print("STAGE 3 - BUILDING LLM PROMPT")
        print("=" * 70)

        prompt = build_prompt(
            labels,
            annotations,
            evidence
        )

        print(prompt)

        print("\n" + "=" * 70)
        print("STAGE 4 - CALLING OLLAMA / MISTRAL")
        print("=" * 70)

        print(
            f"POST {OLLAMA_URL} "
            f"model={OLLAMA_MODEL}"
        )

        diagnosis = ask_ollama(prompt)

        print("\n" + "=" * 70)
        print("STAGE 5 - AI RESPONSE RECEIVED")
        print("=" * 70)

        print(diagnosis)

        results.append({
            "alert": labels.get("alertname"),
            "namespace": namespace,
            "pod": pod,
            "diagnosis": diagnosis
        })

    print("\n" + "=" * 70)
    print("STAGE 6 - WEBHOOK PROCESSING COMPLETE")
    print("=" * 70)

    return {
        "status": "processed",
        "results": results
    }, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)