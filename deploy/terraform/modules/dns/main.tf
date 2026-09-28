# DNS-записи во внутреннем BIND (RFC2136, TSIG). Публичные имена (parking.example.uz) — та же зона, split-horizon.
terraform {
  required_providers {
    dns = { source = "hashicorp/dns", version = "~> 3.4" }
  }
}
variable "zone" { type = string }
variable "records" { type = map(list(string)) } # имя → адреса
variable "ttl" {
  type    = number
  default = 300
}

resource "dns_a_record_set" "this" {
  for_each  = var.records
  zone      = "${var.zone}."
  name      = each.key
  addresses = each.value
  ttl       = var.ttl
}
output "fqdns" { value = { for k, v in var.records : k => "${k}.${var.zone}" } }
