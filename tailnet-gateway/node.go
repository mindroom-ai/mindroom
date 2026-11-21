package main

import (
	"context"
	"errors"
	"net"
	"strings"

	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/tsnet"
)

// Each link has a persistent node that only listens on the operator's tailnet.
type tsnetNode struct {
	*tsnet.Server
	whoIs    func(context.Context, string) (*apitype.WhoIsResponse, error)
	listener net.Listener
}

func NewTsnetNode(loginServer, authKey string, tags []string) (NodeFactory, error) {
	authKey = strings.TrimSpace(authKey)
	if authKey == "" {
		return nil, errors.New("TAILNET_SSH_AUTH_KEY is not set")
	}
	return func(dir, hostname string) (_ Node, err error) {
		server := &tsnet.Server{
			Dir:           dir,
			Hostname:      hostname,
			ControlURL:    loginServer,
			AuthKey:       authKey,
			AdvertiseTags: tags,
			// tsnet can still emit interactive sign-in messages after a key fails.
			UserLogf: func(string, ...any) {},
		}
		defer func() {
			if err != nil {
				server.Close()
				err = errors.New(strings.ReplaceAll(err.Error(), authKey, "[redacted]"))
			}
		}()
		// Listen starts the node without waiting for it to enroll.
		listener, err := server.Listen("tcp", ":22")
		if err != nil {
			return nil, err
		}
		client, err := server.LocalClient()
		if err != nil {
			return nil, err
		}
		return &tsnetNode{server, client.WhoIs, listener}, nil
	}, nil
}

func (n *tsnetNode) Listener() net.Listener { return n.listener }

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
