# The tailnet SSH gateway: one static binary on an image without a shell.
# Build from the repository root: docker build -f tailnet-gateway/Dockerfile .
FROM mirror.gcr.io/library/golang:1.27.4 AS build

WORKDIR /src
COPY tailnet-gateway/go.mod tailnet-gateway/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY tailnet-gateway/ ./
ARG VERSION=0.0.0
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /tailnet-gateway .

# The node keys' directory belongs to the image's user, so a new named volume mounted there does too.
RUN mkdir /state

FROM gcr.io/distroless/static:nonroot

COPY --from=build /tailnet-gateway /tailnet-gateway
COPY --from=build --chown=nonroot:nonroot /state /state

ENTRYPOINT ["/tailnet-gateway"]
