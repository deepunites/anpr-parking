# production: 3 узла RKE2 (по одному на каждый хост Proxmox = 3 зоны отказа), 3 ВМ PostgreSQL/etcd, 2 PgBouncer+HAProxy,
# 2 MinIO (site replication), 1 verify-ВМ, 1 LB (HAProxy/keepalived перед ingress). Production — та же схема, больше ресурсов.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    proxmox = { source = "bpg/proxmox", version = "~> 0.84" }
    dns     = { source = "hashicorp/dns", version = "~> 3.4" }
    minio   = { source = "aminueza/minio", version = "~> 3.4" }
    tls     = { source = "hashicorp/tls", version = "~> 4.0" }
    vault   = { source = "hashicorp/vault", version = "~> 5.0" }
  }
  backend "s3" {
    bucket                      = "tfstate"
    key                         = "parking/production.tfstate"
    region                      = "uz-tashkent-1"
    endpoints                   = { s3 = "https://minio.infra.parking.uz:9000" }
    use_lockfile                = true # блокировка через S3 conditional writes (без DynamoDB)
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    use_path_style              = true
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = false
}
provider "dns" {
  update {
    server        = var.dns_server
    key_name      = "tsig-terraform."
    key_algorithm = "hmac-sha256"
    key_secret    = var.dns_tsig_secret
  }
}
provider "minio" {
  minio_server   = var.minio_server
  minio_user     = var.minio_user
  minio_password = var.minio_password
  minio_ssl      = true
}
provider "vault" {
  address = var.vault_address
}

locals {
  env      = "production"
  zones    = ["pve-a", "pve-b", "pve-c"] # хосты Proxmox = зоны отказа
  gateway  = "10.10.0.1"
  ssh_keys = var.ssh_keys
  rke2_user_data = templatefile("${path.module}/../../templates/rke2-cloud-init.yaml.tftpl", {
    token       = var.rke2_token
    server_url  = "https://k8s-api.${var.zone}:9345"
    cni         = "canal" # NetworkPolicy из коробки (§2)
    cis_profile = "cis"
    node_labels = { "topology.kubernetes.io/zone" = "$ZONE" }
  })
  k8s_nodes = { for i, z in local.zones : "k8s-${local.env}-${i + 1}" => { node = z, ip = "10.10.10.${11 + i}/24", zone = z } }
  pg_nodes  = { for i, z in local.zones : "pg-${local.env}-${i + 1}" => { node = z, ip = "10.10.20.${11 + i}/24", role = i == 0 ? "primary" : (i == 1 ? "sync" : "async") } }
}

module "k8s" {
  for_each    = local.k8s_nodes
  source      = "../../modules/vm"
  name        = each.key
  node        = each.value.node
  template_id = var.template_id
  cpu         = 8
  memory_mb   = 32768
  disk_gb     = 120
  ip          = each.value.ip
  gateway     = local.gateway
  vlan        = 20
  ssh_keys    = local.ssh_keys
  user_data   = replace(local.rke2_user_data, "$ZONE", each.value.zone)
  tags        = ["k8s", local.env, each.value.zone]
}

module "postgres" {
  for_each     = local.pg_nodes
  source       = "../../modules/vm"
  name         = each.key
  node         = each.value.node
  template_id  = var.template_id
  cpu          = 8
  memory_mb    = 32768 # shared_buffers 8 ГБ / effective_cache_size 24 ГБ (INFRA §3)
  disk_gb      = 60
  data_disk_gb = 500 # NVMe под data_dir + WAL
  ip           = each.value.ip
  gateway      = local.gateway
  vlan         = 20
  ssh_keys     = local.ssh_keys
  tags         = ["postgres", local.env, "patroni-${each.value.role}", each.value.node]
}

module "pgbouncer" {
  for_each    = { "pgb-${local.env}-1" = { node = local.zones[0], ip = "10.10.20.21/24" }, "pgb-${local.env}-2" = { node = local.zones[1], ip = "10.10.20.22/24" } }
  source      = "../../modules/vm"
  name        = each.key
  node        = each.value.node
  template_id = var.template_id
  cpu         = 2
  memory_mb   = 4096
  disk_gb     = 40
  ip          = each.value.ip
  gateway     = local.gateway
  vlan        = 20
  ssh_keys    = local.ssh_keys
  tags        = ["pgbouncer", local.env]
}

module "redis" {
  # Redis 7 + Sentinel (кворум 2/3) на трёх зонах: PARKING_REDIS_URL = redis+sentinel://r1:26379,r2:26379,r3:26379/parking (INFRA §2, фаза 10)
  for_each    = { for i, z in local.zones : "redis-${local.env}-${i + 1}" => { node = z, ip = "10.10.21.${11 + i}/24" } }
  source      = "../../modules/vm"
  name        = each.key
  node        = each.value.node
  template_id = var.template_id
  cpu         = 2
  memory_mb   = 4096
  disk_gb     = 40
  ip          = each.value.ip
  gateway     = local.gateway
  vlan        = 21
  ssh_keys    = local.ssh_keys
  tags        = ["redis", local.env, each.value.node]
}

module "minio" {
  for_each     = { "minio-${local.env}-1" = { node = local.zones[1], ip = "10.10.22.11/24" }, "minio-${local.env}-2" = { node = local.zones[2], ip = "10.10.22.12/24" } }
  source       = "../../modules/vm"
  name         = each.key
  node         = each.value.node
  template_id  = var.template_id
  cpu          = 4
  memory_mb    = 8192
  disk_gb      = 40
  data_disk_gb = 16000 # COST §3: 2 кадра × 300 КБ × 260 тыс. сессий/сут × 90 дней ≈ 14 ТБ (+ версии); 2 ТБ хватало на ≈ 13 дней
  ip           = each.value.ip
  gateway      = local.gateway
  vlan         = 22
  ssh_keys     = local.ssh_keys
  tags         = ["minio", local.env]
}

module "verify" {
  source       = "../../modules/vm"
  name         = "verify-${local.env}-1"
  node         = local.zones[2] # не там, где primary
  template_id  = var.template_id
  cpu          = 4
  memory_mb    = 8192
  disk_gb      = 40
  data_disk_gb = 500
  ip           = "10.10.23.11/24"
  gateway      = local.gateway
  vlan         = 23
  ssh_keys     = local.ssh_keys
  tags         = ["backup_verify", local.env]
}

module "buckets" {
  source = "../../modules/minio_buckets"
  env    = local.env
}

module "pki" {
  source          = "../../modules/pki"
  env             = local.env
  allowed_domains = [var.zone, "svc.cluster.local"]
}

module "dns" {
  source = "../../modules/dns"
  zone   = var.zone
  records = {
    "k8s-api"    = [for n in module.k8s : n.ip]
    "production" = [var.ingress_vip]
    "pgbouncer"  = [for n in module.pgbouncer : n.ip]
    "redis"      = [for n in module.redis : n.ip]
    "minio"      = [for n in module.minio : n.ip]
  }
}

output "ansible_inventory" {
  value = templatefile("${path.module}/../../templates/inventory.ini.tftpl", {
    env       = local.env
    postgres  = { for k, v in module.postgres : k => { ip = v.ip, role = local.pg_nodes[k].role } }
    pgbouncer = { for k, v in module.pgbouncer : k => v.ip }
    redis     = { for k, v in module.redis : k => v.ip }
    minio     = { for k, v in module.minio : k => v.ip }
    verify    = { (module.verify.name) = module.verify.ip }
  })
}
output "k8s_nodes" { value = { for k, v in module.k8s : k => { ip = v.ip, zone = v.node } } }
output "ca_cert_pem" { value = module.pki.ca_cert_pem }
