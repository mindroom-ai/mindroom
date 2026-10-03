# MindRoom MatrixRTC Chart

This optional chart deploys the backend that Matrix group calls need: a single-node [LiveKit SFU](https://github.com/livekit/livekit) and the [MatrixRTC authorization service](https://github.com/mindroom-ai/lk-jwt-service) (`lk-jwt-service`).
Use it with the `mindroom-client` chart, which publishes MatrixRTC discovery and proxies the public `/livekit/jwt` and `/livekit/sfu` routes to these Services.
MindRoom voice agents and Element Call-compatible clients then use the same backend; see [Voice Calls](../../../docs/voice-calls.md) for the agent configuration.
The chart leaves public HTTPS ingress, TLS, TURN, and the static media IP reservation to your cluster and provider.

## Minimal Install

Create a Secret with a LiveKit API key and secret shared by both components:

```bash
kubectl -n mindroom create secret generic matrixrtc-keys \
  --from-literal=LIVEKIT_KEY="$(openssl rand -hex 8)" \
  --from-literal=LIVEKIT_SECRET="$(openssl rand -hex 32)"
```

Reserve a static public IPv4 address with your provider, then install:

```yaml
keys:
  existingSecret: matrixrtc-keys

livekit:
  media:
    loadBalancerIP: 203.0.113.10

auth:
  livekitUrl: wss://matrix.example.com/livekit/sfu
  fullAccessHomeservers:
    - example.com
```

```bash
helm upgrade --install matrixrtc ./cluster/k8s/matrixrtc \
  --namespace mindroom \
  -f matrixrtc-values.yaml
```

`auth.livekitUrl` is the public WebSocket URL of the LiveKit signaling route; the authorization service returns it to clients with every token.
`auth.fullAccessHomeservers` lists the Matrix server names whose users may create calls; users from other homeservers can only join calls that already exist.
The chart renders LiveKit with `room.auto_create: false`, so rooms are only created through the authorization service.

## API Keys

Both components read the key and secret from `keys.existingSecret` through `keys.apiKeyKey` and `keys.apiSecretKey`.
The authorization service receives them as `LIVEKIT_KEY` and `LIVEKIT_SECRET`.
LiveKit receives `LIVEKIT_KEYS` built from the same two values through Kubernetes dependent environment variable expansion, so the Secret needs no separate keys file and the key material never lands in the ConfigMap.
Use letters and digits only, because LiveKit parses the combined value as a quoted YAML `"key": "secret"` pair, and use a secret of at least 32 characters.
Secret changes do not roll the pods; restart both Deployments after rotating keys.

## Media Networking

WebRTC media does not travel through the HTTP proxy.
Clients connect directly to TCP `livekit.media.ports.tcp` and UDP `livekit.media.ports.udp` on the address LiveKit advertises.
The chart uses one value, `livekit.media.loadBalancerIP`, both as the `loadBalancerIP` of the `LoadBalancer` media Service and as LiveKit's `rtc.node_ip`, so the advertised address and the load balancer cannot drift apart.

- Reserve the address as a static public IP in the same region as the cluster before installing, so recreating the Service keeps it.
- The media Service publishes the ports unchanged, because clients connect to exactly the ports LiveKit advertises.
- The Service mixes TCP and UDP ports, which requires a load balancer implementation with mixed-protocol support.
  Select it with `livekit.media.loadBalancerClass` or `livekit.media.annotations`, for example `loadBalancerClass: networking.gke.io/l4-regional-external` on GKE, or use an implementation such as MetalLB on bare metal.
- Allow inbound TCP and UDP on both ports to the address in any firewall your provider does not configure automatically for `LoadBalancer` Services.
- The chart configures no TURN server, so clients on networks that block both media ports cannot join until you add one, for example an external server through `rtc.turn_servers` in `livekit.extraConfig`.

## Pairing With mindroom-client

Enable MatrixRTC discovery and the same-origin proxy in the client chart, pointing at this chart's Services:

```yaml
matrix:
  homeserverUrl: https://matrix.example.com

matrixRTC:
  enabled: true
  livekitServiceUrl: https://matrix.example.com/livekit/jwt
  proxy:
    enabled: true
    jwtServiceUpstream: http://matrixrtc-mindroom-matrixrtc-auth.mindroom.svc.cluster.local:8080
    sfuUpstream: http://matrixrtc-mindroom-matrixrtc-livekit.mindroom.svc.cluster.local:7880
```

The Service names follow `<release>-mindroom-matrixrtc-auth` and `<release>-mindroom-matrixrtc-livekit`; set `fullnameOverride` for shorter names.
The client chart must serve `/.well-known/matrix/client` for the Matrix server name so clients and MindRoom discover `livekitServiceUrl`.

## Network Policy

Set `networkPolicy.enabled: true` to restrict ingress to both components:

```yaml
networkPolicy:
  enabled: true
  clientPodSelector:
    matchLabels:
      app.kubernetes.io/name: mindroom-client
```

Pods matching `clientPodSelector` in the release namespace may reach the signaling and authorization ports.
`networkPolicy.extraFrom` adds raw `NetworkPolicyPeer` entries, for example an ingress controller namespace that routes the public paths directly to these Services.
The authorization service calls LiveKit's server API through the public `auth.livekitUrl`, so those requests also arrive through the proxy.
The media ports stay open to every source because clients connect to them directly.
The policies select only ingress; egress stays unrestricted because the authorization service must reach the users' homeservers and the public LiveKit URL.

## Customization

- `livekit.extraConfig` is merged over the rendered LiveKit `config.yaml` for options such as `webhook`, `limit`, `prometheus`, or `rtc.turn_servers`.
  It cannot set the chart-owned `port`, `keys`, `key_file`, `rtc.node_ip`, `rtc.use_external_ip`, `rtc.tcp_port`, `rtc.udp_port`, `rtc.port_range_start`, `rtc.port_range_end`, or `room.auto_create`, because the Services, probes, NetworkPolicies, and room access rules depend on them.
- `livekit.extraEnv` and `auth.extraEnv` append raw environment variables, for example `LIVEKIT_CS_API_URL_OVERRIDES` or `LIVEKIT_REDIS_URL` for the authorization service.
- Both pods run as an unprivileged user with a read-only root filesystem and no service account token; adjust `podSecurityContext` and `securityContext` to match your policy.

## Notes

- LiveKit runs as one replica with a `Recreate` strategy because single-node LiveKit keeps rooms in memory and the media address targets one pod.
- Both pods disable Service links, because LiveKit parses `LIVEKIT_*` environment variables as config flags and a Service named `livekit` would inject an unparseable `LIVEKIT_PORT`.
- The LiveKit image is pinned to a release tag; the authorization service image defaults to `latest` with `pullPolicy: Always`, so pin `auth.image.tag` or `auth.image.digest` for reproducible deployments.
- A `checksum/config` pod annotation rolls LiveKit whenever the rendered config changes.
