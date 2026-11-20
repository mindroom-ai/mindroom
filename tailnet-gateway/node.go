package main

import (
	"context"
	"log"
	"net"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tsnet"
)

// tsnetNode is a link's machine in a real tailnet: one tsnet server that listens on the tailnet's :22 only.
type tsnetNode struct {
	server   *tsnet.Server
	client   *local.Client
	listener net.Listener
	// stop ends the watch of the node's bus.
	stop context.CancelFunc

	mu       sync.Mutex
	observed NodeObserved
}

// NewTsnetNode returns the factory of real nodes. They enrol at loginServer, or at Tailscale's own for "", with
// authKey, or by the login URL they report for "", and advertise tags.
func NewTsnetNode(loginServer, authKey string, tags []string) NodeFactory {
	return func(dir, hostname string) (Node, error) {
		server := &tsnet.Server{
			Dir:           dir,
			Hostname:      hostname,
			ControlURL:    loginServer,
			AuthKey:       authKey,
			AdvertiseTags: tags,
			// tsnet tells its user the login URL every 5 s, which no log may carry.
			UserLogf: func(string, ...any) {},
		}
		// Never Up, which waits for the login: the node is to report its login URL meanwhile.
		if err := server.Start(); err != nil {
			return nil, err
		}
		client, err := server.LocalClient()
		if err != nil {
			server.Close()
			return nil, err
		}
		listener, err := server.Listen("tcp", ":22")
		if err != nil {
			server.Close()
			return nil, err
		}
		ctx, stop := context.WithCancel(context.Background())
		node := &tsnetNode{
			server:   server,
			client:   client,
			listener: listener,
			stop:     stop,
			observed: NodeObserved{State: "needs_login"},
		}
		// The directory is named by the link's id.
		go node.watch(ctx, filepath.Base(dir))
		return node, nil
	}
}

// watch keeps what the node sees of itself as its bus tells of it, until the node is closed.
func (n *tsnetNode) watch(ctx context.Context, id string) {
	var state ipn.State
	var loginURL *string
	status := &ipnstate.Status{}
	for {
		bus, err := n.client.WatchIPNBus(ctx, ipn.NotifyInitialState)
		for err == nil {
			var told ipn.Notify
			if told, err = bus.Next(); err != nil {
				break
			}
			if told.State != nil {
				state = *told.State
				if state == ipn.Running {
					// The URL was used, and a node that falls back gets a new one.
					loginURL = nil
				}
			}
			if told.BrowseToURL != nil {
				loginURL = told.BrowseToURL
			}
			// The bus tells that something changed, and the status what: the names come with the first netmap.
			if now, err := n.client.Status(ctx); err == nil {
				status = now
			}
			seen := observe(state, loginURL, status)
			n.mu.Lock()
			n.observed = seen
			n.mu.Unlock()
		}
		if bus != nil {
			bus.Close()
		}
		if ctx.Err() != nil {
			return
		}
		log.Printf("link %s: watching its node: %v", id, err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
}

// observe is what a node sees of itself: the state and the login URL as its bus told them, the rest as its status
// has it. Only a node that needs a login reports a URL, and whatever tsnet is doing that is not running or waiting
// for an approval is a node that needs one.
func observe(state ipn.State, loginURL *string, status *ipnstate.Status) NodeObserved {
	seen := NodeObserved{State: "needs_login"}
	switch state {
	case ipn.Running:
		seen.State = "running"
	case ipn.NeedsMachineAuth:
		seen.State = "needs_approval"
	default:
		seen.LoginURL = loginURL
	}
	if status.CurrentTailnet != nil {
		seen.Tailnet = filled(status.CurrentTailnet.MagicDNSSuffix)
	}
	if self := status.Self; self != nil {
		seen.DNSName = filled(strings.TrimSuffix(self.DNSName, "."))
		seen.LoginName = filled(status.User[self.UserID].LoginName)
		if expiry := self.KeyExpiry; expiry != nil && !expiry.IsZero() {
			seen.KeyExpiry = filled(expiry.UTC().Format(time.RFC3339))
		}
	}
	return seen
}

// filled returns the text, or nil for none.
func filled(text string) *string {
	if text == "" {
		return nil
	}
	return &text
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

// peer names the login and the machine a WhoIs answer is of; a tagged device has no login.
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

func (n *tsnetNode) Observed() NodeObserved {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.observed
}

func (n *tsnetNode) Relogin(ctx context.Context) error {
	return n.client.StartLoginInteractive(ctx)
}

func (n *tsnetNode) Logout(ctx context.Context) error {
	return n.client.Logout(ctx)
}

func (n *tsnetNode) Close() error {
	n.stop()
	return n.server.Close()
}
