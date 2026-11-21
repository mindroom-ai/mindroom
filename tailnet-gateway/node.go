package main

import (
	"context"
	"errors"
	"net"
	"strings"

	"tailscale.com/client/local"
	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/tsnet"
)

// Each link has a persistent node that only listens on the operator's tailnet.
type tsnetNode struct {
	server   *tsnet.Server
	client   *local.Client
	listener net.Listener
}

func NewTsnetNode(loginServer, authKey string, tags []string) (NodeFactory, error) {
	if strings.TrimSpace(authKey) == "" {
		return nil, errors.New("TAILNET_SSH_AUTH_KEY is not set")
	}
	return func(dir, hostname string) (Node, error) {
		server := tsnetServer(dir, hostname, loginServer, authKey, tags)
		// Listen starts the node without waiting for it to enroll.
		listener, err := server.Listen("tcp", ":22")
		if err != nil {
			return nil, errors.New(strings.ReplaceAll(err.Error(), authKey, "[redacted]"))
		}
		client, _ := server.LocalClient() // Listen already started the server.
		return &tsnetNode{server, client, listener}, nil
	}, nil
}

func tsnetServer(dir, hostname, loginServer, authKey string, tags []string) *tsnet.Server {
	return &tsnet.Server{
		Dir:           dir,
		Hostname:      hostname,
		ControlURL:    loginServer,
		AuthKey:       authKey,
		AdvertiseTags: tags,
		// tsnet can still emit interactive sign-in messages after a key fails.
		UserLogf: func(string, ...any) {},
	}
}

func (n *tsnetNode) Listener() net.Listener { return n.listener }

func (n *tsnetNode) WhoIs(ctx context.Context, remoteAddr string) (string, string, error) {
	who, err := n.client.WhoIs(ctx, remoteAddr)
	if err != nil {
		return "", "", err
	}
	login, node := peer(who)
	return login, node, nil
}

func peer(who *apitype.WhoIsResponse) (login, node string) {
	if who.Node == nil {
		return "", ""
	}
	if who.UserProfile != nil && !who.Node.IsTagged() {
		login = who.UserProfile.LoginName
	}
	if node = who.Node.ComputedName; node == "" {
		node = strings.TrimSuffix(who.Node.Name, ".")
	}
	return login, node
}

func (n *tsnetNode) Logout(ctx context.Context) error { return n.client.Logout(ctx) }
func (n *tsnetNode) Close() error                     { return n.server.Close() }
