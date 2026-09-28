# Terraform / OpenTofu — инфраструктура под кластером (фаза 9 §6, CORE Q5 / A-04)

Слой под Kubernetes — виртуализация Proxmox VE с API (провайдер `bpg/proxmox`). Terraform управляет ВМ (узлы RKE2 в трёх
зонах отказа = три хоста Proxmox, ВМ PostgreSQL/etcd, PgBouncer+HAProxy, MinIO, verify-ВМ), сетью (VLAN/bridge, cloud-init),
DNS (RFC2136 → внутренний BIND), TLS (внутренний CA + Vault PKI; публичные сертификаты — cert-manager в кластере),
объектным хранилищем (бакеты MinIO: `pg-backups` с object-lock и версионированием, `parking-media`, `tfstate`).
Конфигурацию ВМ доводит Ansible (`deploy/ansible`, inventory генерируется из outputs).

- Состояние: S3-бэкенд в MinIO (бакет `tfstate`, версионирование) с блокировкой `use_lockfile = true` (OpenTofu/Terraform ≥ 1.10,
  без DynamoDB). Три окружения = три ключа состояния и три набора кредов (`envs/<env>`), production применяется только с
  раннера production после approval (cd.yml), из CI по расписанию — только `plan` (drift.yml).
- Проверка без облака: `tofu validate` + `tofu test` (mock_provider) — envs/staging/tests/plan.tftest.hcl.
- Если под кластером окажется bare metal (A-04) — модуль `vm` заменяется Ansible-инвентарём, остальные модули не меняются.

```
cd envs/staging && tofu init && tofu plan -out plan.bin && tofu apply plan.bin
tofu output -raw ansible_inventory > ../../ansible/inventory.staging.ini
```
