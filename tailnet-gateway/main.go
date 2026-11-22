// The tailnet SSH gateway listens on one node per link and pipes TCP to the worker named by the primary.
// It never dials through the tailnet or talks to Docker or Kubernetes.
package main

import (
	"cmp"
	"context"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"tailscale.com/envknob"
	"tailscale.com/logtail"
	"tailscale.com/net/netns"
)

// version is the release the image was built for.
var version = "0.0.0"

func main() {
	tags := strings.FieldsFunc(os.Getenv("TAILNET_SSH_TAGS"), func(r rune) bool { return r == ',' || r == ' ' })
	nodes, err := NewTsnetNode(os.Getenv("TAILNET_SSH_LOGIN_SERVER"), os.Getenv("TAILNET_SSH_AUTH_KEY"), tags)
	if err != nil {
		log.Fatal(err)
	}
	// Trim like the primary does, so a Secret written with a trailing newline still matches.
	token := strings.TrimSpace(os.Getenv("TAILNET_SSH_GATEWAY_TOKEN"))
	if token == "" {
		log.Fatal("TAILNET_SSH_GATEWAY_TOKEN is not set")
	}
	primaryURL := cmp.Or(os.Getenv("TAILNET_SSH_PRIMARY_URL"), "http://127.0.0.1:8769")
	stateDir := cmp.Or(os.Getenv("TAILNET_SSH_STATE_DIR"), "/state")

	// Don't persist or upload node logs, or bind sockets to the default-route interface (which may be loopback).
	envknob.SetNoLogsNoSupport()
	logtail.Disable()
	netns.SetEnabled(false)
	// tsnet looks for a directory for logs it never writes, and without this makes one in the home directory.
	os.Setenv("TS_LOGS_DIR", stateDir)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.Printf("tailnet gateway %s: nodes in %s", version, stateDir)
	NewGateway(NewPrimary(primaryURL, token), nodes, stateDir).Run(ctx)
}
