package main

import (
	"context"
	"log"
	"net"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

const (
	reconcileEvery = 5 * time.Second
	statusEvery    = 30 * time.Second // the primary expires activity after 90 s
	countedAfter   = 75 * time.Second // past sshd's LoginGraceTime, not proof of authentication
)

var linkID = regexp.MustCompile(`^[0-9a-f]{16}$`)

type Node interface {
	Listener() net.Listener
	WhoIs(context.Context, string) (login, node string, err error)
	Close() error
}

type NodeFactory func(dir, hostname string) (Node, error)

type linkNode struct {
	Node
	logins []string               // nil when omitted; read and updated under Gateway.mu
	open   map[net.Conn]time.Time // admitted, zero until piping to sshd
	recent [connectsAMinute]time.Time
	next   int
}

type Gateway struct {
	primary  *Primary
	factory  NodeFactory
	stateDir string
	now      func() time.Time
	after    func(time.Duration) <-chan time.Time
	dial     func(ctx context.Context, network, address string) (net.Conn, error)
	mu       sync.Mutex
	nodes    map[string]*linkNode
}

func NewGateway(p *Primary, f NodeFactory, stateDir string) *Gateway {
	return &Gateway{primary: p, factory: f, stateDir: stateDir, now: time.Now, after: time.After,
		dial: (&net.Dialer{}).DialContext, nodes: map[string]*linkNode{}}
}

func (g *Gateway) Run(ctx context.Context) {
	go g.poll(ctx, statusEvery, func(ctx context.Context) error { return g.primary.PostStatus(ctx, g.Status()) })
	g.poll(ctx, reconcileEvery, g.Reconcile)
	for _, node := range g.nodes {
		node.Close()
	}
}

func (g *Gateway) poll(ctx context.Context, interval time.Duration, call func(context.Context) error) {
	for ctx.Err() == nil {
		if err := call(ctx); err != nil && ctx.Err() == nil {
			log.Printf("gateway poll (%s): primary request failed", interval)
		}
		select {
		case <-ctx.Done():
		case <-g.after(interval):
		}
	}
}

// Only Reconcile changes the node set, so a state directory cannot be reused during Close.
func (g *Gateway) Reconcile(ctx context.Context) error {
	links, err := g.primary.Links(ctx)
	g.mu.Lock()
	if err == nil {
		for _, node := range g.nodes {
			node.logins = nil
		}
		for _, link := range links {
			if node := g.nodes[link.ID]; node != nil {
				node.logins = append([]string{}, link.AllowedLogins...)
			}
		}
	}
	for id, node := range g.nodes {
		if node.logins == nil && len(node.open) == 0 {
			delete(g.nodes, id)
			g.mu.Unlock()
			node.Close() // keep its state on disk
			g.mu.Lock()
		}
	}
	g.mu.Unlock()
	for _, link := range links {
		id := link.ID
		if g.nodes[id] != nil || !linkID.MatchString(id) || ctx.Err() != nil {
			continue
		}
		node, err := g.factory(filepath.Join(g.stateDir, id), link.Hostname)
		if err != nil {
			log.Printf("link %s (%s): starting node: %v", id, link.Hostname, err)
			continue
		}
		live := &linkNode{Node: node, logins: append([]string{}, link.AllowedLogins...), open: map[net.Conn]time.Time{}}
		g.mu.Lock()
		g.nodes[id] = live
		g.mu.Unlock()
		go g.serve(ctx, id, live)
	}
	return err
}

func (g *Gateway) Status() map[string]int {
	g.mu.Lock()
	defer g.mu.Unlock()
	counts, now := map[string]int{}, g.now()
	for id, node := range g.nodes {
		for _, reached := range node.open {
			if !reached.IsZero() && now.Sub(reached) >= countedAfter {
				counts[id]++
			}
		}
	}
	return counts
}
