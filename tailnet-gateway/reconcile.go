package main

import (
	"context"
	"log"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"sync"
	"time"
)

const (
	reconcileEvery  = 5 * time.Second
	statusEvery     = 30 * time.Second // the primary expires activity after 90 s
	nodeCallTimeout = 10 * time.Second
	countedAfter    = 75 * time.Second // beyond sshd's LoginGraceTime, not proof of authentication
)

var linkID = regexp.MustCompile(`^[0-9a-f]{16}$`)

type Node interface {
	Listener() net.Listener
	WhoIs(ctx context.Context, remoteAddr string) (login, node string, err error)
	Logout(ctx context.Context) error
	Close() error
}

type NodeFactory func(dir, hostname string) (Node, error)

type held struct {
	cancel  context.CancelFunc
	reached time.Time // zero until the connection reaches sshd
}

type clock interface {
	Now() time.Time
	After(time.Duration) <-chan time.Time
}

type systemClock struct{}

func (systemClock) Now() time.Time                         { return time.Now() }
func (systemClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

type Gateway struct {
	primary  Primary
	factory  NodeFactory
	stateDir string
	clock    clock
	dial     func(context.Context, string) (net.Conn, error)

	// Reconcile alone writes links, nodes and removed. Other goroutines read them under mu.
	mu       sync.Mutex
	links    map[string]DesiredLink
	nodes    map[string]Node
	removed  map[string]bool
	conns    map[string]map[net.Conn]*held
	connects map[string][]time.Time
}

func NewGateway(p Primary, f NodeFactory, stateDir string) *Gateway {
	return &Gateway{
		primary: p, factory: f, stateDir: stateDir, clock: systemClock{}, dial: dialWorker,
		nodes: map[string]Node{}, removed: map[string]bool{},
		conns: map[string]map[net.Conn]*held{}, connects: map[string][]time.Time{},
	}
}

// Run reconciles serially. The status publisher never waits for a pass, and vice versa.
func (g *Gateway) Run(ctx context.Context) {
	changed, done := make(chan struct{}, 1), make(chan struct{})
	go func() {
		defer close(done)
		var posted StatusReport
		var postedAt time.Time
		tick := g.clock.After(reconcileEvery)
		for ctx.Err() == nil {
			select {
			case <-ctx.Done():
				return
			case <-changed:
			case <-tick:
				tick = g.clock.After(reconcileEvery)
			}
			status, now := g.Status(), g.clock.Now()
			if reflect.DeepEqual(status, posted) && now.Sub(postedAt) < statusEvery {
				continue
			}
			if err := g.primary.PostStatus(ctx, status); err != nil {
				log.Printf("status: %v", err)
				posted = StatusReport{} // retry at the next tick or pass
			} else {
				posted, postedAt = status, now
			}
		}
	}()
	defer func() { <-done }()
	for ctx.Err() == nil {
		if err := g.Reconcile(ctx); err != nil {
			log.Printf("reconcile: %v", err)
		}
		select {
		case changed <- struct{}{}:
		default:
		}
		select {
		case <-ctx.Done():
		case <-g.clock.After(reconcileEvery):
		}
	}
}

func (g *Gateway) Reconcile(ctx context.Context) error {
	answer, err := g.primary.Links(ctx)
	if err != nil {
		return err
	}
	listed := make(map[string]DesiredLink, len(answer))
	for _, link := range answer {
		if !linkID.MatchString(link.ID) || (link.Desired != "on" && link.Desired != "removing") {
			log.Printf("ignoring link %q with desired state %q", link.ID, link.Desired)
			continue
		}
		listed[link.ID] = link
	}

	// Revoke admissions and cancel pipes before any slow node operation.
	g.mu.Lock()
	var revoked []net.Conn
	for id, open := range g.conns {
		if link := listed[id]; link.Desired != "on" || link.AccessRevision != g.links[id].AccessRevision {
			for conn, kept := range open {
				kept.cancel()
				revoked = append(revoked, conn)
			}
			delete(g.conns, id)
		}
	}
	g.links = listed
	for id := range g.connects {
		if listed[id].Desired != "on" {
			delete(g.connects, id)
		}
	}
	for id := range g.removed {
		if listed[id].Desired != "removing" {
			delete(g.removed, id)
		}
	}
	g.mu.Unlock()
	for _, conn := range revoked {
		conn.Close()
	}

	for id := range g.nodes {
		if _, ok := listed[id]; !ok {
			g.closeNode(id, g.drop(id)) // keep its state for a later listing
		}
	}
	for id, link := range listed {
		if link.Desired == "on" && g.nodes[id] == nil {
			if node := g.startNode(link); node != nil {
				g.mu.Lock()
				g.nodes[id] = node
				g.mu.Unlock()
				go g.serve(ctx, id, node)
			}
		}
	}
	for id, link := range listed {
		if link.Desired == "removing" && !g.removed[id] && g.remove(ctx, link) {
			g.mu.Lock()
			g.removed[id] = true
			g.mu.Unlock()
		}
	}
	return nil
}

func (g *Gateway) startNode(link DesiredLink) Node {
	node, err := g.factory(filepath.Join(g.stateDir, link.ID), link.Hostname)
	if err != nil {
		log.Printf("link %s: starting node %q: %v", link.ID, link.Hostname, err)
		return nil
	}
	log.Printf("link %s: started node %q", link.ID, link.Hostname)
	return node
}

func (g *Gateway) drop(id string) Node {
	g.mu.Lock()
	defer g.mu.Unlock()
	node := g.nodes[id]
	delete(g.nodes, id)
	return node
}

func (g *Gateway) closeNode(id string, node Node) {
	if err := node.Close(); err != nil {
		log.Printf("link %s: closing node: %v", id, err)
	}
}

func (g *Gateway) remove(ctx context.Context, link DesiredLink) bool {
	dir := filepath.Join(g.stateDir, link.ID)
	node := g.drop(link.ID)
	if node == nil {
		if _, err := os.Lstat(dir); err == nil {
			node = g.startNode(link)
		}
	}
	if node != nil {
		logoutCtx, cancel := context.WithTimeout(ctx, nodeCallTimeout)
		if err := node.Logout(logoutCtx); err != nil {
			log.Printf("link %s: logging out: %v", link.ID, err)
		}
		cancel()
		g.closeNode(link.ID, node)
	}
	if err := os.RemoveAll(dir); err != nil {
		log.Printf("link %s: deleting state: %v", link.ID, err)
		return false
	}
	return true
}

func (g *Gateway) Status() StatusReport {
	g.mu.Lock()
	defer g.mu.Unlock()
	status := StatusReport{OpenConnections: map[string]int{}, Removed: make([]string, 0, len(g.removed))}
	now := g.clock.Now()
	for id, open := range g.conns {
		for _, kept := range open {
			if !kept.reached.IsZero() && now.Sub(kept.reached) >= countedAfter {
				status.OpenConnections[id]++
			}
		}
	}
	for id := range g.removed {
		status.Removed = append(status.Removed, id)
	}
	slices.Sort(status.Removed)
	return status
}
