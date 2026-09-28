# Бакеты MinIO: pg-backups (object lock + версионирование, репликация во второй MinIO), parking-media, tfstate.
terraform {
  required_providers {
    minio = { source = "aminueza/minio", version = "~> 3.4" }
  }
}
variable "env" { type = string }
variable "media_retention_days" {
  type    = number
  default = 30
}

resource "minio_s3_bucket" "pg_backups" {
  bucket         = "pg-backups-${var.env}"
  acl            = "private"
  object_locking = true # защита репозитория pgBackRest от удаления/шифровальщика
}
resource "minio_s3_bucket_versioning" "pg_backups" {
  bucket = minio_s3_bucket.pg_backups.bucket
  versioning_configuration { status = "Enabled" }
}
resource "minio_s3_bucket" "media" {
  bucket = "parking-media-${var.env}"
  acl    = "private"
}
resource "minio_s3_bucket_versioning" "media" {
  bucket = minio_s3_bucket.media.bucket
  versioning_configuration { status = "Enabled" }
}
resource "minio_ilm_policy" "media" {
  bucket = minio_s3_bucket.media.bucket
  rule {
    id         = "expire-frames"
    expiration = "${var.media_retention_days}d"
  }
}
resource "minio_s3_bucket" "tfstate" {
  bucket = "tfstate"
  acl    = "private"
}
resource "minio_s3_bucket_versioning" "tfstate" {
  bucket = minio_s3_bucket.tfstate.bucket
  versioning_configuration { status = "Enabled" }
}
output "buckets" { value = { backups = minio_s3_bucket.pg_backups.bucket, media = minio_s3_bucket.media.bucket } }
