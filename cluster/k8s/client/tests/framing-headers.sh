#!/usr/bin/env bash
# Every response of the chart-managed nginx says who may frame it: the device pairing page no
# origin, everything else only the client's own origin, which frames its own pages such as the
# PDF viewer's frame page and client plugin pages under public/plugins/.

set -euo pipefail

chart_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="$(mktemp -d)"
container_name="client-chart-framing-test-$$"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rm -rf "$work_dir"
}
trap cleanup EXIT

fail() {
  echo "$*" >&2
  exit 1
}

cat > "$work_dir/values.yaml" <<'EOF'
image:
  tag: v0.0.0
matrix:
  homeserverUrl: https://matrix.example.com
matrixRTC:
  enabled: true
  livekitServiceUrl: https://matrix.example.com/livekit/jwt
  proxy:
    enabled: true
    jwtServiceUpstream: http://127.0.0.1:9
    sfuUpstream: http://127.0.0.1:9
authenticationRecovery:
  enabled: true
EOF

render_conf() {
  local output="$1"
  shift
  helm template framing-test "$chart_dir" \
    --namespace example \
    --values "$work_dir/values.yaml" \
    "$@" \
    | yq -r 'select(.data["default.conf"]) | .data["default.conf"]' \
    > "$output"
}

# Prints one line per problem: the server block must send the framing headers, which locations
# without add_header of their own inherit, and a location that sets add_header must repeat them.
check_conf() {
  awk '
    function expected(block) { return block ~ /connect\)?\/\?\$/ ? "none" : "self" }
    function check(where, policy, csp, xfo) {
      if (policy == "none") { want_csp = "\"frame-ancestors '\''none'\''\""; want_xfo = "\"DENY\"" }
      else { want_csp = "\"frame-ancestors '\''self'\''\""; want_xfo = "\"SAMEORIGIN\"" }
      if (csp != want_csp) print where ": Content-Security-Policy " (csp ? csp : "missing") ", want " want_csp
      if (xfo != want_xfo) print where ": X-Frame-Options " (xfo ? xfo : "missing") ", want " want_xfo
    }
    /^server \{/ { in_server = 1; depth = 1; next }
    !in_server { next }
    {
      line = $0
      opens = gsub(/\{/, "", line)
      closes = gsub(/\}/, "", line)
      if (depth == 1 && $1 ~ /^(location|if)$/) {
        block = $0; sub(/^ +/, "", block); sub(/ *\{$/, "", block)
        in_block = 1; sets_headers = 0; csp = ""; xfo = ""
      } else if ($1 == "add_header") {
        value = $0; sub(/^ *add_header [^ ]+ /, "", value); sub(/ always;$/, "", value)
        if (in_block) {
          sets_headers = 1
          if ($2 == "Content-Security-Policy") csp = value
          if ($2 == "X-Frame-Options") xfo = value
        } else {
          if ($2 == "Content-Security-Policy") server_csp = value
          if ($2 == "X-Frame-Options") server_xfo = value
        }
      }
      depth += opens - closes
      if (in_block && depth == 1) {
        if (sets_headers || expected(block) == "none") check(block, expected(block), csp, xfo)
        in_block = 0
      }
      if (depth == 0) { check("server", "self", server_csp, server_xfo); in_server = 0 }
    }
  ' "$1"
}

render_conf "$work_dir/root.conf"
render_conf "$work_dir/chat.conf" --set basePath=/chat --set rootServiceWorkerCleanup.enabled=true
for conf in root chat; do
  problems="$(check_conf "$work_dir/$conf.conf")"
  test -z "$problems" || fail "$conf.conf framing headers:"$'\n'"$problems"
done

html="$work_dir/html"
mkdir -p "$html/assets" "$html/public/locales" "$html/public/plugins/example" \
  "$html/public/element-call/assets" "$html/.well-known"
printf '%s\n' '<!doctype html><title>client</title>' > "$html/index.html"
printf '%s\n' '<!doctype html><title>pdf frame</title>' > "$html/pdf-frame.html"
printf '%s\n' '<!doctype html><title>plugin frame</title>' > "$html/public/plugins/example/frame.html"
printf '%s\n' '<!doctype html><title>call</title>' > "$html/public/element-call/index.html"
printf '%s\n' '{}' | tee "$html/config.json" "$html/version.json" "$html/manifest.json" \
  "$html/public/locales/en.json" "$html/.well-known/apple-app-site-association" >/dev/null
printf '%s\n' '"use strict";' | tee "$html/sw.js" "$html/root-cleanup-sw.js" "$html/pdf.worker.min.js" \
  "$html/authentication-recovery.js" "$html/assets/app-1a2b.js" \
  "$html/public/element-call/assets/call-1a2b.js" >/dev/null

# Each request: the path, then the expected framing policy.
root_requests=(
  "/ self" "/rooms/!room:example.com self" "/pdf-frame.html self" "/connect none" "/connect/ none"
  "/CONNECT none" "/Connect/ none"
  "/config.json self" "/version.json self" "/notices.json self" "/manifest.json self" "/sw.js self"
  "/assets/app-1a2b.js self" "/assets/missing.js self" "/rooms/x/assets/app-1a2b.js self"
  "/public/locales/en.json self" "/public/plugins/example/frame.html self"
  "/public/element-call/index.html self" "/public/element-call/assets/call-1a2b.js self"
  "/runtime-config.js self" "/runtime-config.js/x self" "/authentication-recovery.js self"
  "/authentication-recovery-probe self" "/.well-known/matrix/client self"
  "/.well-known/apple-app-site-association self" "/livekit/jwt/sfu/get self"
)
chat_requests=(
  "/ self" "/chat self" "/chat/ self" "/chat/rooms/!room:example.com self" "/chat/connect none"
  "/chat/connect/ none" "/chat/Connect none" "/chat/%43ONNECT/ none" "/chat/connection self"
  "/chat/config.json self" "/chat/version.json self" "/chat/notices.json self"
  "/chat/manifest.json self" "/chat/pdf.worker.min.js self" "/chat/sw.js self" "/chat/sw.js/x self"
  "/sw.js self" "/chat/assets/app-1a2b.js self" "/chat/public/locales/en.json self"
  "/chat/public/plugins/example/frame.html self" "/chat/public/element-call/index.html self"
  "/chat/public/element-call/assets/call-1a2b.js self" "/runtime-config.js self"
  "/.well-known/matrix/client self" "/livekit/jwt/sfu/get self"
)

check_responses() {
  local conf="$1"
  shift
  docker run --detach --rm \
    --name "$container_name" \
    --publish 127.0.0.1::8080 \
    --volume "$work_dir/$conf.conf:/etc/nginx/conf.d/default.conf:ro" \
    --volume "$html:/usr/share/nginx/html:ro" \
    nginx:alpine >/dev/null
  local port
  port="$(docker port "$container_name" 8080/tcp | sed -E 's/.*:([0-9]+)$/\1/')"
  curl --silent --output /dev/null --retry 50 --retry-connrefused --retry-delay 0 --retry-max-time 10 \
    "http://127.0.0.1:$port/runtime-config.js" || fail "nginx did not start with the $conf config"
  local request path policy headers want_csp want_xfo
  for request in "$@"; do
    path="${request% *}"
    policy="${request##* }"
    headers="$(curl --silent --output /dev/null --dump-header - "http://127.0.0.1:$port$path" | tr -d '\r')"
    if [ "$policy" = none ]; then
      want_csp="frame-ancestors 'none'"
      want_xfo="DENY"
    else
      want_csp="frame-ancestors 'self'"
      want_xfo="SAMEORIGIN"
    fi
    grep -Fxq "Content-Security-Policy: $want_csp" <<< "$headers" \
      || fail "$conf $path: expected Content-Security-Policy: $want_csp"$'\n'"$headers"
    grep -Fxq "X-Frame-Options: $want_xfo" <<< "$headers" \
      || fail "$conf $path: expected X-Frame-Options: $want_xfo"$'\n'"$headers"
    test "$(grep -ci '^content-security-policy:' <<< "$headers")" = 1 \
      || fail "$conf $path: expected one Content-Security-Policy header"$'\n'"$headers"
    test "$(grep -ci '^x-frame-options:' <<< "$headers")" = 1 \
      || fail "$conf $path: expected one X-Frame-Options header"$'\n'"$headers"
  done
  docker rm -f "$container_name" >/dev/null
}

check_responses root "${root_requests[@]}"
check_responses chat "${chat_requests[@]}"

echo "client chart framing header tests passed"
