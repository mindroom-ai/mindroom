module "dns" {
  count               = var.enable_dns ? 1 : 0
  source              = "./modules/dns"
  domain              = var.domain
  environment         = var.environment
  porkbun_api_key     = var.porkbun_api_key
  porkbun_secret_key  = var.porkbun_secret_key
  ipv4_address        = module.kube-hetzner.control_plane_nodes[0].ipv4_address
  ipv6_address        = module.kube-hetzner.control_plane_nodes[0].ipv6_address
}
