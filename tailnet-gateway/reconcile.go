package main

import (
	"context"
	"log"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sync"
	"time"
)

const (
	reconcileEvery = 5 * time.Second
	// The primary ignores a status older than 90 s, so an unchanged one is posted again this often.
	statusEvery = 30 * time.Second
	// How long logging a node in again, or out, may take.
	nodeCallTimeout = 10 * time.Second
	// A connection this old is past sshd's LoginGraceTime, so it is an authenticated session and counts as open.
	countedAfter = 75 * time.Second
)

// A link id names a state directory, so nothing but the primary's 16 hex characters is one.
var linkID = regexp.MustCompile(`^[0-9a-f]{16}$`)

// NodeObserved is what a node sees of itself in its tailnet.
type NodeObserved struct {
	// State is "needs_login", "needs_approval" or "running".
	State     string
	LoginURL  *string
	Tailnet   *string
	DNSName   *string
	LoginName *string
	KeyExpiry *string
}

// Node is one link's machine in its person's tailnet.
type Node interface {
	// Listener is the node's tailnet :22 listener.
	Listener() net.Listener
	// WhoIs names the tailnet peer behind a connection's remote address; login is "" for a tagged device.
	WhoIs(ctx context.Context, remoteAddr string) (login, node string, err error)
	Observed() NodeObserved
	// Relogin asks for a new login URL, which a node that needs a login does not get by itself.
	Relogin(ctx context.Context) error
	// Logout removes the node from its tailnet.
	Logout(ctx context.Context) error
	Close() error
}

// NodeFactory starts the node whose state is in dir, under hostname.
type NodeFactory func(dir, hostname string) (Node, error)

type clock interface {
	Now() time.Time
	After(d time.Duration) <-chan time.Time
}

type systemClock struct{}

func (systemClock) Now() time.Time                         { return time.Now() }
func (systemClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

// Gateway holds one node per link the primary lists, in <stateDir>/<link id>.
//
// It decides nothing: which nodes run and which state is deleted is the primary's word, read by Reconcile, and Run
// alone posts what the nodes see back to it.
type Gateway struct {
	primary    Primary
	factory    NodeFactory
	stateDir   string
	enrollment string
	clock      clock

	// mu guards the maps below; no call to a node, the primary or the disk is made under it, except Observed.
	mu sync.Mutex
	// links is what the primary listed last, by link id.
	links map[string]DesiredLink
	nodes map[string]Node
	// relogin holds the links whose node gets a Relogin once it needs a login: it ran, or the last Relogin failed.
	relogin map[string]bool
	// removed holds the links listed as removing whose state is gone.
	removed map[string]bool
	// conns holds each link's open connections and when this clock saw them accepted.
	conns map[string]map[net.Conn]time.Time
	// lastError holds what the primary answered when it last refused a connection of the link.
	lastError map[string]string
}

// NewGateway returns a gateway that holds no node until its first Reconcile; enrollment is "login" or "auth_key".
func NewGateway(p Primary, f NodeFactory, stateDir, enrollment string) *Gateway {
	return &Gateway{
		primary:    p,
		factory:    f,
		stateDir:   stateDir,
		enrollment: enrollment,
		clock:      systemClock{},
		links:      map[string]DesiredLink{},
		nodes:      map[string]Node{},
		relogin:    map[string]bool{},
		removed:    map[string]bool{},
		conns:      map[string]map[net.Conn]time.Time{},
		lastError:  map[string]string{},
	}
}

// Run reconciles at once and every 5 s until ctx ends, and posts the status when it changed or 30 s passed since
// the last post: after each pass, and every 5 s besides, so a pass that is held up holds no report up.
//
// The passes run beside this loop, which is the only caller of PostStatus, so no two reports are ever under way at
// once: the primary keeps each report whole, and one that overtook a later one would undo it.
func (g *Gateway) Run(ctx context.Context) {
	// Each pass is handed over with its error, and its report is over before the wait for the next begins.
	passed := make(chan error)
	reported := make(chan struct{})
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		for {
			err := g.Reconcile(ctx)
			select {
			case passed <- err:
			case <-ctx.Done():
				return
			}
			select {
			case <-reported:
			case <-ctx.Done():
				return
			}
			select {
			case <-g.clock.After(reconcileEvery):
			case <-ctx.Done():
				return
			}
		}
	}()
	defer func() { <-stopped }()

	// Until a pass read the links the gateway holds none of its nodes, and a report without them would tell the
	// primary they are gone.
	read := false
	// What the primary last took, and when; nil while it has to be told again.
	var posted map[string]LinkStatus
	var postedAt time.Time
	report := func() {
		if !read {
			return
		}
		status := g.Status()
		if posted != nil && reflect.DeepEqual(status, posted) && g.clock.Now().Sub(postedAt) < statusEvery {
			return
		}
		if err := g.primary.PostStatus(ctx, g.enrollment, status); err != nil {
			log.Printf("status: %v", err)
			posted = nil
			return
		}
		posted, postedAt = status, g.clock.Now()
	}
	tick := g.clock.After(reconcileEvery)
	for {
		select {
		case <-ctx.Done():
			return
		case err := <-passed:
			if err != nil {
				log.Printf("reconcile: %v", err)
			} else {
				read = true
			}
			report()
			select {
			case reported <- struct{}{}:
			case <-ctx.Done():
				return
			}
		case <-tick:
			report()
			tick = g.clock.After(reconcileEvery)
		}
	}
}

// Reconcile brings the nodes in line with the primary's links, once.
//
// A link that is on or suspended has its node running. A link that is removing is logged out and its state deleted.
// A node the primary does not list is stopped and its state kept, so an empty answer destroys no enrolment, and a
// pass that cannot read the links returns the error and changes nothing.
func (g *Gateway) Reconcile(ctx context.Context) error {
	answer, err := g.primary.Links(ctx)
	if err != nil {
		return err
	}
	listed := make(map[string]DesiredLink, len(answer))
	removed := map[string]bool{}
	for _, link := range answer {
		if !linkID.MatchString(link.ID) {
			log.Printf("ignoring a link whose id is not 16 hex characters: %q", link.ID)
			continue
		}
		switch link.Desired {
		case "on", "suspended":
			g.keep(ctx, link)
		case "removing":
			if g.remove(ctx, link) {
				removed[link.ID] = true
			}
		default:
			log.Printf("link %s: ignoring the unknown desired state %q", link.ID, link.Desired)
			continue
		}
		listed[link.ID] = link
	}

	g.mu.Lock()
	var unlisted []string
	for id := range g.nodes {
		if _, ok := listed[id]; !ok {
			unlisted = append(unlisted, id)
		}
	}
	for id := range g.lastError {
		if _, ok := listed[id]; !ok {
			delete(g.lastError, id)
		}
	}
	g.links, g.removed = listed, removed
	g.mu.Unlock()
	for _, id := range unlisted {
		if node := g.drop(id); node != nil {
			if err := node.Close(); err != nil {
				log.Printf("link %s: closing its node: %v", id, err)
			}
		}
	}
	return nil
}

// keep runs the link's node, and asks a node that needs a login for a new login URL when it fell back from running
// or the link's login_after moved.
func (g *Gateway) keep(ctx context.Context, link DesiredLink) {
	g.mu.Lock()
	node := g.nodes[link.ID]
	// A link that has a node was listed by the pass that started it.
	moved := g.links[link.ID].LoginAfter != link.LoginAfter
	owed := g.relogin[link.ID]
	g.mu.Unlock()
	if node == nil {
		// Never the hostname: two nodes on one directory overwrite each other's state.
		started, err := g.factory(filepath.Join(g.stateDir, link.ID), link.Hostname)
		if err != nil {
			log.Printf("link %s: starting its node: %v", link.ID, err)
			return
		}
		g.mu.Lock()
		g.nodes[link.ID] = started
		g.mu.Unlock()
		// A node asks for its first login itself.
		return
	}
	switch node.Observed().State {
	case "running":
		owed = true
	case "needs_login":
		if owed || moved {
			reloginCtx, cancel := context.WithTimeout(ctx, nodeCallTimeout)
			err := node.Relogin(reloginCtx)
			cancel()
			if err != nil {
				log.Printf("link %s: asking for a new login: %v", link.ID, err)
			}
			owed = err != nil
		}
	}
	g.mu.Lock()
	g.relogin[link.ID] = owed
	g.mu.Unlock()
}

// remove closes the link's connections, logs its node out, and deletes its state, and says whether the state is
// gone. The node of a link that has state but is not running is started to be logged out.
func (g *Gateway) remove(ctx context.Context, link DesiredLink) bool {
	dir := filepath.Join(g.stateDir, link.ID)
	node := g.drop(link.ID)
	if node == nil {
		if _, err := os.Lstat(dir); err == nil {
			started, err := g.factory(dir, link.Hostname)
			if err != nil {
				log.Printf("link %s: starting its node to log it out: %v", link.ID, err)
			} else {
				node = started
			}
		}
	}
	if node != nil {
		logoutCtx, cancel := context.WithTimeout(ctx, nodeCallTimeout)
		if err := node.Logout(logoutCtx); err != nil {
			// The node keys are deleted next, so the machine left in the tailnet is one nobody can be.
			log.Printf("link %s: logging its node out: %v", link.ID, err)
		}
		cancel()
		if err := node.Close(); err != nil {
			log.Printf("link %s: closing its node: %v", link.ID, err)
		}
	}
	if err := os.RemoveAll(dir); err != nil {
		log.Printf("link %s: deleting its state: %v", link.ID, err)
		return false
	}
	return true
}

// drop closes the link's connections and returns its node, which the gateway no longer holds, or nil without one.
func (g *Gateway) drop(id string) Node {
	g.mu.Lock()
	node := g.nodes[id]
	open := g.conns[id]
	delete(g.nodes, id)
	delete(g.relogin, id)
	delete(g.conns, id)
	g.mu.Unlock()
	for conn := range open {
		conn.Close()
	}
	return node
}

// Status returns what Run reports to the primary: every node the gateway holds as its tailnet sees it, and every
// link whose removal is done as removed. It does not wait for a pass under way.
func (g *Gateway) Status() map[string]LinkStatus {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := g.clock.Now()
	status := make(map[string]LinkStatus, len(g.nodes)+len(g.removed))
	for id, node := range g.nodes {
		seen := node.Observed()
		link := LinkStatus{
			State:     seen.State,
			LoginURL:  seen.LoginURL,
			Tailnet:   seen.Tailnet,
			DNSName:   seen.DNSName,
			LoginName: seen.LoginName,
			KeyExpiry: seen.KeyExpiry,
		}
		for _, opened := range g.conns[id] {
			if now.Sub(opened) >= countedAfter {
				link.OpenConnections++
			}
		}
		if detail, ok := g.lastError[id]; ok {
			link.LastError = &detail
		}
		status[id] = link
	}
	for id := range g.removed {
		status[id] = LinkStatus{State: "removed"}
	}
	return status
}
