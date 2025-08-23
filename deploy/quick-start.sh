#!/usr/bin/env bash
# Quick start script for Mindroom instances

set -e

INSTANCE=${1:-try}

if [ "$INSTANCE" == "try" ]; then
    BACKEND_PORT=8765
    FRONTEND_PORT=3005
    MATRIX_PORT=8449
    DOMAIN="try.mindroom.chat"
elif [ "$INSTANCE" == "alt" ]; then
    BACKEND_PORT=8766
    FRONTEND_PORT=3006  
    MATRIX_PORT=8450
    DOMAIN="alt.mindroom.chat"
else
    echo "Unknown instance: $INSTANCE"
    exit 1
fi

DATA_DIR="/home/basnijholt/mindroom/deploy/instance_data/$INSTANCE"
ENV_FILE="/home/basnijholt/mindroom/deploy/.env.$INSTANCE"

echo "Starting $INSTANCE instance..."

# Export environment
export DATA_DIR
export INSTANCE_NAME=$INSTANCE
export INSTANCE_DOMAIN=$DOMAIN
export BACKEND_PORT
export FRONTEND_PORT
export MATRIX_PORT

# Load env file
if [ -f "$ENV_FILE" ]; then
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

# Start containers
cd /home/basnijholt/mindroom/deploy

echo "Starting backend..."
docker run -d \
    --name ${INSTANCE}-backend \
    --network mynetwork \
    -v ${DATA_DIR}:/data \
    --env-file ${ENV_FILE} \
    -e DATA_DIR=/data \
    -e BACKEND_PORT=${BACKEND_PORT} \
    --label "traefik.enable=true" \
    --label "traefik.http.routers.${INSTANCE}-backend.rule=Host(\`${DOMAIN}\`) && PathPrefix(\`/api\`)" \
    --label "traefik.http.routers.${INSTANCE}-backend.entrypoints=websecure" \
    --label "traefik.http.routers.${INSTANCE}-backend.tls.certresolver=porkbun" \
    --label "traefik.http.services.${INSTANCE}-backend.loadbalancer.server.port=8765" \
    deploy-mindroom-backend:latest

echo "Starting frontend..."
docker run -d \
    --name ${INSTANCE}-frontend \
    --network mynetwork \
    -v ${DATA_DIR}:/data \
    --env-file ${ENV_FILE} \
    -e DATA_DIR=/data \
    -e NEXT_PUBLIC_API_BASE_URL=https://${DOMAIN}/api \
    -e NEXT_PUBLIC_MATRIX_SERVER_NAME=m-${DOMAIN} \
    -p ${FRONTEND_PORT}:3003 \
    --label "traefik.enable=true" \
    --label "traefik.http.routers.${INSTANCE}-frontend.rule=Host(\`${DOMAIN}\`)" \
    --label "traefik.http.routers.${INSTANCE}-frontend.entrypoints=websecure" \
    --label "traefik.http.routers.${INSTANCE}-frontend.tls.certresolver=porkbun" \
    --label "traefik.http.services.${INSTANCE}-frontend.loadbalancer.server.port=3005" \
    deploy-mindroom-frontend:latest

echo "Starting Tuwunel..."
docker run -d \
    --name ${INSTANCE}-tuwunel \
    --network mynetwork \
    -v ${DATA_DIR}/tuwunel:/var/db/tuwunel \
    -e TUWUNEL_SERVER_NAME=m-${DOMAIN} \
    --label "traefik.enable=true" \
    --label "traefik.http.routers.${INSTANCE}-matrix.rule=Host(\`m-${DOMAIN}\`)" \
    --label "traefik.http.routers.${INSTANCE}-matrix.entrypoints=websecure" \
    --label "traefik.http.routers.${INSTANCE}-matrix.tls.certresolver=porkbun" \
    --label "traefik.http.services.${INSTANCE}-matrix.loadbalancer.server.port=8448" \
    ghcr.io/matrix-construct/tuwunel:latest

echo "✅ Started $INSTANCE instance"
echo "   Frontend: https://${DOMAIN}"
echo "   Matrix: https://m-${DOMAIN}"