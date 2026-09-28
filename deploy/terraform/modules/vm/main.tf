# ВМ Proxmox из cloud-init шаблона (Ubuntu 24.04). Зона отказа = хост Proxmox (target_node); tags → inventory Ansible.
terraform {
  required_providers {
    proxmox = { source = "bpg/proxmox", version = "~> 0.84" }
  }
}

variable "name" { type = string }
variable "node" { type = string }        # хост Proxmox = зона отказа
variable "template_id" { type = number } # VMID cloud-init шаблона
variable "cpu" { type = number }
variable "memory_mb" { type = number }
variable "disk_gb" { type = number }
variable "data_disk_gb" {
  type    = number
  default = 0
}                               # отдельный диск под данные (PostgreSQL/MinIO), storage быстрый NVMe
variable "ip" { type = string } # CIDR, напр. 10.10.20.11/24
variable "gateway" { type = string }
variable "vlan" { type = number }
variable "ssh_keys" { type = list(string) }
variable "user_data" {
  type    = string
  default = ""
} # cloud-init user-data (RKE2 install и т. п.)
variable "tags" {
  type    = list(string)
  default = []
}
variable "storage" {
  type    = string
  default = "local-nvme"
}

resource "proxmox_virtual_environment_vm" "this" {
  name      = var.name
  node_name = var.node
  tags      = concat(["terraform"], var.tags)
  on_boot   = true

  clone {
    vm_id = var.template_id
    full  = true
  }
  cpu {
    cores = var.cpu
    type  = "host"
  }
  memory {
    dedicated = var.memory_mb
  }
  agent { enabled = true }
  disk {
    datastore_id = var.storage
    interface    = "scsi0"
    size         = var.disk_gb
    discard      = "on"
    iothread     = true
  }
  dynamic "disk" {
    for_each = var.data_disk_gb > 0 ? [1] : []
    content {
      datastore_id = var.storage
      interface    = "scsi1"
      size         = var.data_disk_gb
      discard      = "on"
      iothread     = true
    }
  }
  network_device {
    bridge  = "vmbr0"
    vlan_id = var.vlan
    model   = "virtio"
  }
  initialization {
    datastore_id = var.storage
    ip_config {
      ipv4 {
        address = var.ip
        gateway = var.gateway
      }
    }
    user_account {
      username = "ops"
      keys     = var.ssh_keys
    }
    user_data_file_id = var.user_data != "" ? proxmox_virtual_environment_file.user_data[0].id : null
  }
  lifecycle {
    ignore_changes = [initialization[0].user_data_file_id] # переустановка cloud-init не должна пересоздавать ВМ
  }
}

resource "proxmox_virtual_environment_file" "user_data" {
  count        = var.user_data != "" ? 1 : 0
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.node
  source_raw {
    data      = var.user_data
    file_name = "${var.name}-user-data.yaml"
  }
}

output "ip" { value = split("/", var.ip)[0] }
output "name" { value = var.name }
output "node" { value = var.node }
