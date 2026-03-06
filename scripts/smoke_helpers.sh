#!/usr/bin/env bash

wait_for_http_match() {
  local url="$1"
  local expected="$2"
  local label="$3"
  local attempts="${4:-30}"
  local sleep_seconds="${5:-2}"

  for _ in $(seq 1 "${attempts}"); do
    if curl -fsS "${url}" | grep -q "${expected}"; then
      echo "[smoke] ${label} ready"
      return 0
    fi
    sleep "${sleep_seconds}"
  done

  echo "[error] Timed out waiting for ${label} (${url})" >&2
  return 1
}
