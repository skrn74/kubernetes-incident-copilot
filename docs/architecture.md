# Architecture

```text
CrashLoopBackOff -> kube-state-metrics -> Prometheus -> Alertmanager
                                                   |
                                                   v
                                            Flask webhook
                                             /         \
                                    Kubernetes API    Ollama/Mistral
                                             \         /
                                              diagnosis
                                                  |
                                               Human SRE
```

## Design boundary

Detection is deterministic and metric-driven. The Python service collects read-only evidence. The local LLM interprets supplied evidence and remains advisory; remediation is human-controlled.
