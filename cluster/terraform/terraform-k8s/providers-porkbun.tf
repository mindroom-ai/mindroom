provider "porkbun" {
  # Credentials are passed via variables and up.sh exports TF_VAR_*
  api_key    = var.porkbun_api_key
  secret_key = var.porkbun_secret_key
}
