#!/usr/bin/env bash
set -euo pipefail
! rg -n '(?:from|import) mindroom\.memory\._|from mindroom\.memory\.(?:_prompting|_policy|_shared|auto_flush|functions|config)\b|import mindroom\.memory\.(?:_prompting|_policy|_shared|auto_flush|functions|config)\b(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?' src --glob '!src/mindroom/memory/**'
