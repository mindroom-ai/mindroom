module "dns" {
  source             = "./modules/dns"
  superdomain        = var.domain      # The full platform domain (e.g., staging.mindroom.chat)
  root_domain        = var.root_domain # The DNS root domain (e.g., mindroom.chat)
  porkbun_api_key    = var.porkbun_api_key
  porkbun_secret_key = var.porkbun_secret_key
  ipv4_address       = module.kube-hetzner.control_plane_nodes[0].ipv4_address
  ipv6_address       = module.kube-hetzner.control_plane_nodes[0].ipv6_address
}
