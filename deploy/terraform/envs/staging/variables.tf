variable "zone" {
  type    = string
  default = "staging.parking.uz"
}
variable "proxmox_endpoint" { type = string }
variable "proxmox_api_token" {
  type      = string
  sensitive = true
}
variable "template_id" {
  type    = number
  default = 9000
}
variable "ssh_keys" { type = list(string) }
variable "dns_server" { type = string }
variable "dns_tsig_secret" {
  type      = string
  sensitive = true
}
variable "minio_server" { type = string }
variable "minio_user" {
  type      = string
  sensitive = true
}
variable "minio_password" {
  type      = string
  sensitive = true
}
variable "vault_address" { type = string }
variable "rke2_token" {
  type      = string
  sensitive = true
}
variable "ingress_vip" {
  type    = string
  default = "10.20.10.100"
}
