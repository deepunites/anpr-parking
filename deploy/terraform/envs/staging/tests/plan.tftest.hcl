# tofu test: план с моками провайдеров (без Proxmox/Vault/MinIO) — проверяет граф, количество ВМ по зонам, теги.
mock_provider "proxmox" {
  mock_resource "proxmox_virtual_environment_file" {
    defaults = {
      id = "local:snippets/user-data.yaml" # провайдер валидирует формат идентификатора файла
    }
  }
}
mock_provider "dns" {}
mock_provider "minio" {}
mock_provider "vault" {}
mock_provider "tls" {}

variables {
  proxmox_endpoint  = "https://pve.example.test:8006"
  proxmox_api_token = "user@pve!token=00000000-0000-0000-0000-000000000000"
  ssh_keys          = ["ssh-ed25519 AAAA test"]
  dns_server        = "10.20.0.53"
  dns_tsig_secret   = "c2VjcmV0"
  minio_server      = "minio.test:9000"
  minio_user        = "u"
  minio_password    = "p"
  vault_address     = "https://vault.test:8200"
  rke2_token        = "t"
}

run "three_zones_for_k8s_and_postgres" {
  command = plan
  assert {
    condition     = length(distinct([for k, n in module.k8s : n.node])) == 3
    error_message = "узлы Kubernetes должны стоять в трёх зонах отказа (§2)"
  }
  assert {
    condition     = length(distinct([for k, n in module.postgres : n.node])) == 3
    error_message = "primary, sync и async standby — на разных хостах"
  }
  assert {
    condition     = length(module.pgbouncer) == 2 && length(module.minio) == 2
    error_message = "PgBouncer и MinIO — по две ВМ"
  }
  assert {
    condition     = length(distinct([for k, n in module.redis : n.node])) == 3
    error_message = "Redis + Sentinel — три ВМ в трёх зонах (кворум 2/3)"
  }
}

run "backups_bucket_is_locked_and_versioned" {
  command = plan
  assert {
    condition     = module.buckets.buckets.backups == "pg-backups-staging"
    error_message = "бакет бэкапов окружения"
  }
}
