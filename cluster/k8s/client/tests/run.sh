#!/usr/bin/env bash

set -euo pipefail

chart_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="$(mktemp -d)"
container_name="client-chart-recovery-test-$$"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rm -rf "$work_dir"
}
trap cleanup EXIT

fail() {
  echo "$*" >&2
  exit 1
}

assert_contains() {
  local file="$1"
  local expected="$2"
  rg -F --quiet "$expected" "$file" || fail "expected $file to contain: $expected"
}

assert_not_contains() {
  local file="$1"
  local unexpected="$2"
  if rg -F --quiet "$unexpected" "$file"; then
    fail "expected $file not to contain: $unexpected"
  fi
}

render() {
  local output="$1"
  shift
  helm template recovery-test "$chart_dir" \
    --namespace example \
    "$@" \
    > "$output"
}

expect_render_failure() {
  local expected="$1"
  shift
  local error_file="$work_dir/render-error"
  if render /dev/null "$@" 2> "$error_file"; then
    fail "expected Helm rendering to fail: $expected"
  fi
  assert_contains "$error_file" "$expected"
}

render "$work_dir/default.yaml"
assert_not_contains "$work_dir/default.yaml" "__AUTHENTICATION_RECOVERY_CONFIG__"
assert_not_contains "$work_dir/default.yaml" "location = /authentication-recovery.js"
assert_not_contains "$work_dir/default.yaml" "location = /authentication-recovery-probe"

render "$work_dir/enabled.yaml" \
  --set authenticationRecovery.enabled=true \
  --set basePath=/chat
assert_contains "$work_dir/enabled.yaml" 'window.__AUTHENTICATION_RECOVERY_CONFIG__ = {\"navigationUrl\":\"\",\"probeUrl\":\"/authentication-recovery-probe\",\"timeoutMs\":5000}'
render "$work_dir/explicit.yaml" \
  --set authenticationRecovery.enabled=true \
  --set basePath=/chat \
  --set-string authenticationRecovery.navigationUrl=/chat/login
assert_contains "$work_dir/explicit.yaml" 'window.__AUTHENTICATION_RECOVERY_CONFIG__ = {\"navigationUrl\":\"/chat/login\",\"probeUrl\":\"/authentication-recovery-probe\",\"timeoutMs\":5000}'
assert_contains "$work_dir/enabled.yaml" 'new URL(\"authentication-recovery.js\", document.currentScript.src).href'
assert_contains "$work_dir/enabled.yaml" 'location = /authentication-recovery.js'
assert_contains "$work_dir/enabled.yaml" 'location = /authentication-recovery-probe'

render "$work_dir/custom.yaml" \
  --set authenticationRecovery.enabled=true \
  --set-string 'authenticationRecovery.probeUrl=/session/check?next="chat"&source=<client>' \
  --set-string 'authenticationRecovery.navigationUrl=/chat/?next="room"&source=<client>' \
  --set authenticationRecovery.timeoutMs=1234
assert_contains "$work_dir/custom.yaml" 'location = /authentication-recovery-probe'
assert_not_contains "$work_dir/custom.yaml" 'location = /session/check'
render "$work_dir/min-timeout.yaml" \
  --set authenticationRecovery.enabled=true \
  --set authenticationRecovery.timeoutMs=1000
render "$work_dir/max-timeout.yaml" \
  --set authenticationRecovery.enabled=true \
  --set authenticationRecovery.timeoutMs=30000

for setting in probeUrl navigationUrl; do
  for suffix in '?return=/../room' '?return=%2Froom%5Cthread' '#/../room' '#%2Froom%5Cthread'; do
    render "$work_dir/query-fragment.yaml" \
      --set authenticationRecovery.enabled=true \
      --set-string "authenticationRecovery.$setting=/probe$suffix"
  done
  for url in '/probe/../room?return=/room' '/%2froom?return=/room' '/%5croom#room' '/%2e%2e/room'; do
    expect_render_failure "authenticationRecovery.$setting must be a safe same-origin root-relative URL" \
      --set authenticationRecovery.enabled=true \
      --set-string "authenticationRecovery.$setting=$url"
  done
  for url in '/probe?return=$request_uri' '/probe?return=%0a' '/probe#%7f'; do
    expect_render_failure "authenticationRecovery.$setting must be a safe same-origin root-relative URL" \
      --set authenticationRecovery.enabled=true \
      --set-string "authenticationRecovery.$setting=$url"
  done
done

expect_render_failure "authenticationRecovery requires the chart-managed nginx config" \
  --set authenticationRecovery.enabled=true \
  --set nginx.existingConfigMap=custom-nginx
expect_render_failure "authenticationRecovery.probeUrl must be a safe same-origin root-relative URL" \
  --set authenticationRecovery.enabled=true \
  --set-string authenticationRecovery.probeUrl=//example.com/check
printf '%s\n' \
  'authenticationRecovery:' \
  '  enabled: true' \
  '  navigationUrl: "/chat\\external"' \
  > "$work_dir/backslash-values.yaml"
expect_render_failure "authenticationRecovery.navigationUrl must be a safe same-origin root-relative URL" \
  --values "$work_dir/backslash-values.yaml"
expect_render_failure "authenticationRecovery.navigationUrl must be a safe same-origin root-relative URL" \
  --set authenticationRecovery.enabled=true \
  --set-string authenticationRecovery.navigationUrl=/%2e%2e/admin
expect_render_failure "authenticationRecovery.probeUrl must be a safe same-origin root-relative URL" \
  --set authenticationRecovery.enabled=true \
  --set-string 'authenticationRecovery.probeUrl=/$request_uri'
expect_render_failure "authenticationRecovery.probeUrl must be a safe same-origin root-relative URL" \
  --set authenticationRecovery.enabled=true \
  --set-string $'authenticationRecovery.probeUrl=/check\tpath'
expect_render_failure "authenticationRecovery.probeUrl must be a safe same-origin root-relative URL" \
  --set authenticationRecovery.enabled=true \
  --set-string authenticationRecovery.probeUrl=/%5c%5cexample.com/check
expect_render_failure "authenticationRecovery.timeoutMs must be an integer from 1000 through 30000" \
  --set authenticationRecovery.enabled=true \
  --set authenticationRecovery.timeoutMs=999
expect_render_failure "authenticationRecovery.timeoutMs must be an integer from 1000 through 30000" \
  --set authenticationRecovery.enabled=true \
  --set authenticationRecovery.timeoutMs=30001
expect_render_failure "authenticationRecovery.timeoutMs must be an integer from 1000 through 30000" \
  --set authenticationRecovery.enabled=true \
  --set-string authenticationRecovery.timeoutMs=1.5

yq -r 'select(.data["default.conf"]) | .data["default.conf"]' \
  "$work_dir/custom.yaml" \
  > "$work_dir/default.conf"
mkdir -p "$work_dir/html"
printf '%s\n' 'window.__AUTHENTICATION_RECOVERY__ = { status: "loaded" };' \
  > "$work_dir/html/authentication-recovery.js"
printf '%s\n' '{}' > "$work_dir/html/config.json"
printf '%s\n' '{}' > "$work_dir/html/version.json"
printf '%s\n' '<!doctype html><title>client</title>' > "$work_dir/html/index.html"
printf '%s\n' 'self.addEventListener("fetch", () => {});' > "$work_dir/html/sw.js"

docker run --rm \
  --volume "$work_dir/default.conf:/etc/nginx/conf.d/default.conf:ro" \
  --volume "$work_dir/html:/usr/share/nginx/html:ro" \
  nginx:alpine nginx -t

docker run --detach --rm \
  --name "$container_name" \
  --publish 127.0.0.1::8080 \
  --volume "$work_dir/default.conf:/etc/nginx/conf.d/default.conf:ro" \
  --volume "$work_dir/html:/usr/share/nginx/html:ro" \
  nginx:alpine >/dev/null

port="$(docker port "$container_name" 8080/tcp | sed -E 's/.*:([0-9]+)$/\1/')"
base_url="http://127.0.0.1:$port"
for _ in $(seq 1 50); do
  if curl --fail --silent "$base_url/runtime-config.js" > "$work_dir/runtime-config.js"; then
    break
  fi
  sleep 0.1
done
test -s "$work_dir/runtime-config.js" || fail "nginx did not serve runtime-config.js"

curl --fail --silent --dump-header "$work_dir/runtime.headers" \
  "$base_url/runtime-config.js" > "$work_dir/runtime-config.js"
assert_contains "$work_dir/runtime.headers" "Cache-Control: no-store"
assert_contains "$work_dir/runtime.headers" "Content-Type: application/javascript"
curl --fail --silent --dump-header "$work_dir/asset.headers" \
  "$base_url/authentication-recovery.js" > "$work_dir/asset.js"
assert_contains "$work_dir/asset.headers" "Cache-Control: no-store"
assert_contains "$work_dir/asset.headers" "Content-Type: application/javascript"
cmp "$work_dir/html/authentication-recovery.js" "$work_dir/asset.js"
probe_status="$(curl --silent --output /dev/null --dump-header "$work_dir/probe.headers" \
  --write-out '%{http_code}' "$base_url/authentication-recovery-probe")"
test "$probe_status" = 204 || fail "expected recovery probe status 204, got $probe_status"
assert_contains "$work_dir/probe.headers" "Cache-Control: no-store"

cat > "$work_dir/verify-runtime.mjs" <<'EOF'
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const [runtimePath, runtimeUrl] = process.argv.slice(2);
const source = await readFile(runtimePath, "utf8");
const appended = [];
let configAtAppend;
let readyAtAppend;
let context;
const document = {
  currentScript: { src: runtimeUrl },
  createElement(tagName) {
    assert.equal(tagName, "script");
    return {};
  },
  head: {
    appendChild(script) {
      configAtAppend = context.window.__AUTHENTICATION_RECOVERY_CONFIG__;
      readyAtAppend = context.window.__AUTHENTICATION_RECOVERY_READY__;
      appended.push(script);
    },
  },
};
context = { document, Promise, URL, window: {} };

vm.runInNewContext(source, context);
assert.deepEqual(
  JSON.parse(JSON.stringify(context.window.__AUTHENTICATION_RECOVERY_CONFIG__)),
  {
    navigationUrl: '/chat/?next="room"&source=<client>',
    probeUrl: '/session/check?next="chat"&source=<client>',
    timeoutMs: 1234,
  },
);
assert.equal(appended.length, 1);
assert.equal(configAtAppend, context.window.__AUTHENTICATION_RECOVERY_CONFIG__);
assert.equal(readyAtAppend, context.window.__AUTHENTICATION_RECOVERY_READY__);
assert.equal(
  appended[0].src,
  new URL("authentication-recovery.js", runtimeUrl).href,
);
assert.equal(appended[0].async, false);
assert.equal(appended[0].type, undefined);
assert.equal(typeof appended[0].onload, "function");
assert.equal(typeof appended[0].onerror, "function");

let readyResolved = false;
context.window.__AUTHENTICATION_RECOVERY_READY__.then(() => {
  readyResolved = true;
});
appended[0].onerror();
await Promise.resolve();
assert.equal(readyResolved, true);

vm.runInNewContext(source, context);
assert.equal(appended.length, 1);
EOF

node "$work_dir/verify-runtime.mjs" \
  "$work_dir/runtime-config.js" \
  "$base_url/runtime-config.js"

echo "client chart authentication recovery tests passed"
