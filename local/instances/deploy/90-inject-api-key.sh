#!/bin/sh
# Entrypoint script: if MINDROOM_API_KEY is set, inject an Authorization
# header into the /api proxy block.  Runs after envsubst but before nginx.
CONF="/etc/nginx/conf.d/default.conf"
if [ -n "$MINDROOM_API_KEY" ]; then
    sed -i '/location \/api {/a\        proxy_set_header Authorization "Bearer '"$MINDROOM_API_KEY"'";' "$CONF"
fi
