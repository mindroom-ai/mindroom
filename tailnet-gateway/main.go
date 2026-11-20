// The tailnet SSH gateway holds one tailnet node per link the primary lists, and pipes each node's :22 to the
// worker the primary names for that connection.
//
// It only listens: it has no way to dial a tailnet address, to run a command, or to talk to Docker or Kubernetes.
package main

import (
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
	token := os.Getenv("TAILNET_SSH_GATEWAY_TOKEN")
	if token == "" {
		log.Fatal("TAILNET_SSH_GATEWAY_TOKEN is not set")
	}
	primaryURL := os.Getenv("TAILNET_SSH_PRIMARY_URL")
	if primaryURL == "" {
		primaryURL = "http://127.0.0.1:8769"
	}
	stateDir := os.Getenv("TAILNET_SSH_STATE_DIR")
	if stateDir == "" {
		stateDir = "/state"
	}
	authKey := os.Getenv("TAILNET_SSH_AUTH_KEY")
	enrollment := "login"
	if authKey != "" {
		enrollment = "auth_key"
	}
	tags := strings.FieldsFunc(os.Getenv("TAILNET_SSH_TAGS"), func(r rune) bool { return r == ',' || r == ' ' })

	// Before any node starts: no node log leaves for Tailscale or is kept beside the node's keys, where tsnet would
	// buffer the lines that name the login URL, and no socket is bound to an interface, which without a default
	// route is the loopback, where every dial hangs.
	envknob.SetNoLogsNoSupport()
	logtail.Disable()
	netns.SetEnabled(false)
	// tsnet looks for a directory for logs it never writes, and without this makes one in the home directory.
	os.Setenv("TS_LOGS_DIR", stateDir)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.Printf("tailnet gateway %s: nodes in %s, enrolled by %s", version, stateDir, enrollment)
	nodes := NewTsnetNode(os.Getenv("TAILNET_SSH_LOGIN_SERVER"), authKey, tags)
	NewGateway(NewPrimary(primaryURL, token), nodes, stateDir, enrollment).Run(ctx)
}
