package main

import (
	"context"
	"errors"
	"log"
	"maps"
	"net"
	"path/filepath"
	"slices"
	"strings"

	"tailscale.com/client/local"
	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/health"
	"tailscale.com/ipn"
	"tailscale.com/tsnet"
)

// Each link has a persistent node that only listens on the operator's tailnet.
type tsnetNode struct {
	*tsnet.Server
	whoIs     func(context.Context, string) (*apitype.WhoIsResponse, error)
	listener  net.Listener
	stopWatch context.CancelFunc
	watchDone chan struct{}
}

func NewTsnetNode(loginServer, authKey string, tags []string) (NodeFactory, error) {
	authKey = strings.TrimSpace(authKey)
	if authKey == "" {
		return nil, errors.New("TAILNET_SSH_AUTH_KEY is not set")
	}
	return func(dir, hostname string) (Node, error) {
		server := &tsnet.Server{
			Dir:           dir,
			Hostname:      hostname,
			ControlURL:    loginServer,
			AuthKey:       authKey,
			AdvertiseTags: tags,
			// tsnet can still emit interactive sign-in messages after a key fails.
			UserLogf: func(string, ...any) {},
		}
		// LocalClient starts the node without waiting for it to enroll.
		client, err := server.LocalClient()
		if err == nil {
			var listener net.Listener
			listener, err = server.Listen("tcp", ":22")
			if err == nil {
				watchCtx, stopWatch := context.WithCancel(context.Background())
				watchDone := make(chan struct{})
				logger := &nodeLogger{id: filepath.Base(dir), hostname: hostname, authKey: authKey, logf: log.Printf}
				go func() {
					defer close(watchDone)
					watchNode(watchCtx, client, logger)
				}()
				return &tsnetNode{
					Server:    server,
					whoIs:     client.WhoIs,
					listener:  listener,
					stopWatch: stopWatch,
					watchDone: watchDone,
				}, nil
			}
		}
		server.Close()
		return nil, errors.New(strings.ReplaceAll(err.Error(), authKey, "[redacted]"))
	}, nil
}

func (n *tsnetNode) Listener() net.Listener { return n.listener }

func (n *tsnetNode) Close() error {
	if n.stopWatch != nil {
		n.stopWatch()
	}
	err := n.Server.Close()
	if n.watchDone != nil {
		<-n.watchDone
	}
	return err
}

func watchNode(ctx context.Context, client *local.Client, logger *nodeLogger) {
	watcher, err := client.WatchIPNBus(ctx, ipn.NotifyInitialState|ipn.NotifyInitialHealthState)
	if err != nil {
		if ctx.Err() == nil {
			logger.logf("link %s (%s): watching tsnet state: %s", logger.id, logger.hostname, logger.redact(err.Error()))
		}
		return
	}
	defer watcher.Close()
	stop := context.AfterFunc(ctx, func() { watcher.Close() })
	defer stop()
	for {
		n, err := watcher.Next()
		if err != nil {
			if ctx.Err() == nil {
				logger.logf("link %s (%s): watching tsnet state: %s", logger.id, logger.hostname, logger.redact(err.Error()))
			}
			return
		}
		logger.observe(n)
	}
}

type nodeLogger struct {
	id       string
	hostname string
	authKey  string
	loginURL string
	logf     func(string, ...any)

	hasState bool
	state    ipn.State
	lastErr  string
	warnings map[health.WarnableCode]string
}

func (l *nodeLogger) observe(n ipn.Notify) {
	// Never log BrowseToURL; remember it so other messages cannot echo it either.
	if n.BrowseToURL != nil && *n.BrowseToURL != "" {
		l.loginURL = *n.BrowseToURL
	}
	if n.State != nil {
		state := *n.State
		if !l.hasState || state != l.state {
			l.hasState = true
			l.state = state
			l.logf("link %s (%s): tsnet state: %s", l.id, l.hostname, state)
			if state == ipn.Running {
				l.lastErr = ""
			}
		}
	}
	if n.ErrMessage != nil {
		if msg := l.redact(*n.ErrMessage); msg != "" && msg != l.lastErr {
			l.lastErr = msg
			l.logf("link %s (%s): tsnet error: %s", l.id, l.hostname, msg)
		}
	}
	if n.Health != nil {
		if l.warnings == nil {
			l.warnings = map[health.WarnableCode]string{}
		}
		for code := range l.warnings {
			if _, ok := n.Health.Warnings[code]; !ok {
				delete(l.warnings, code)
			}
		}
		for _, code := range slices.Sorted(maps.Keys(n.Health.Warnings)) {
			warning := n.Health.Warnings[code]
			if warning.Severity == health.SeverityLow {
				delete(l.warnings, code)
				continue
			}
			text := warning.Text
			if text == "" {
				text = warning.Title
			}
			msg := l.redact(text)
			if msg == "" || l.warnings[code] == msg {
				continue
			}
			l.warnings[code] = msg
			l.logf("link %s (%s): tsnet %s: %s", l.id, l.hostname, code, msg)
		}
	}
}

func (l *nodeLogger) redact(msg string) string {
	msg = strings.TrimSpace(msg)
	if l.authKey != "" {
		msg = strings.ReplaceAll(msg, l.authKey, "[redacted]")
	}
	if l.loginURL != "" {
		msg = strings.ReplaceAll(msg, l.loginURL, "[redacted]")
	}
	return msg
}

func (n *tsnetNode) WhoIs(ctx context.Context, remoteAddr string) (login, node string, err error) {
	who, err := n.whoIs(ctx, remoteAddr)
	if err != nil || who == nil || who.Node == nil {
		return "", "", err
	}
	if who.UserProfile != nil && !who.Node.IsTagged() {
		login = who.UserProfile.LoginName
	}
	if node = who.Node.ComputedName; node == "" {
		node = strings.TrimSuffix(who.Node.Name, ".")
	}
	return login, node, nil
}
