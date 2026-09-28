# Внутренний CA для PostgreSQL/etcd/Patroni/PgBouncer TLS и Vault PKI (серверные сертификаты выпускает Vault, ключ CA — offline).
terraform {
  required_providers {
    tls   = { source = "hashicorp/tls", version = "~> 4.0" }
    vault = { source = "hashicorp/vault", version = "~> 5.0" }
  }
}
variable "env" { type = string }
variable "allowed_domains" { type = list(string) }

resource "tls_private_key" "ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P384"
}
resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 87600
  subject { common_name = "parking-${var.env}-ca" }
  allowed_uses = ["cert_signing", "crl_signing"]
}
resource "vault_mount" "pki" {
  path                  = "pki-${var.env}"
  type                  = "pki"
  max_lease_ttl_seconds = 31536000
}
resource "vault_pki_secret_backend_config_ca" "ca" {
  backend    = vault_mount.pki.path
  pem_bundle = "${tls_private_key.ca.private_key_pem}${tls_self_signed_cert.ca.cert_pem}"
}
resource "vault_pki_secret_backend_role" "servers" {
  backend          = vault_mount.pki.path
  name             = "servers"
  allowed_domains  = var.allowed_domains
  allow_subdomains = true
  allow_ip_sans    = true
  max_ttl          = "2160h"
  key_type         = "ec"
  key_bits         = 256
}
# KV v2 для секретов приложения (External Secrets Operator читает kv/parking/<env>/app)
resource "vault_mount" "kv" {
  path = "kv"
  type = "kv-v2"
}
output "ca_cert_pem" { value = tls_self_signed_cert.ca.cert_pem }
