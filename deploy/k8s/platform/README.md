# Платформенный слой кластера (вне чарта приложения)

Ставится один раз на кластер (Ansible/CI job `platform`), в порядке: namespaces → cert-manager → ingress-nginx →
kube-prometheus-stack + prometheus-adapter → External Secrets Operator → Kyverno → Vault (внешний, на ВМ; здесь только
ClusterSecretStore и CronJob снапшотов) → Loki/OTel collector (фаза 7). Версии закреплены в `versions.yaml`.

| Файл | Что |
|---|---|
| `namespaces.yaml` | `parking`, `parking-staging`, `observability`, `ingress-nginx` с PSA `restricted` |
| `external-secrets.yaml` | `ClusterSecretStore vault` (Kubernetes auth, роль `parking-eso`), KV v2 `kv/parking/<env>/app` |
| `prometheus-adapter-values.yaml` | метрика `parking_http_requests_per_second` для HPA API |
| `kyverno-policies.yaml` | только подписанные образы (cosign) из harbor, запрет `latest`/без тега, обязательные probes/resources, non-root |
| `cert-manager-issuer.yaml` | `letsencrypt-dns` (DNS-01 RFC2136) для публичного домена; внутренние TLS — Vault PKI |
| `vault-backup-cronjob.yaml` | ежедневный `vault operator raft snapshot` → MinIO (шифрованный бакет), retention 30 |
