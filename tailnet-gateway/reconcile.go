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
	// A connection that reached sshd this long ago is past its LoginGraceTime, so it is an authenticated session
	// and counts as open.
	countedAfter = 75 * time.Second
	// What the card shows of a link whose node does not start; why is in the gateway's log.
	nodeDidNotStart = "The gateway could not start this link's node."
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

// held is a connection the gateway keeps for a link, from its accept on.
type held struct {
	// cancel ends what the connection waits for: its peer's name, the primary's answer, its worker.
	cancel context.CancelFunc
	// reached is when this clock saw the connection get to its worker's sshd; zero until then.
	reached time.Time
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
	// dial reaches a worker's sshd at the address the primary named.
	dial func(ctx context.Context, address string) (net.Conn, error)

	// mu guards the maps below; no call to a node, the primary or the disk is made under it, except Observed.
	mu sync.Mutex
	// links is what the primary listed last, by link id; a pass replaces it and nothing changes it in place.
	links map[string]DesiredLink
	// started says that a pass has started the node of every link listed as on or suspended; until then the
	// gateway holds none of them, and a report without them would tell the primary they are gone.
	started bool
	nodes   map[string]Node
	// relogin holds the links whose node gets a Relogin once it needs a login: it ran, or the last Relogin failed.
	relogin map[string]bool
	// removed holds the links listed as removing whose state is gone.
	removed map[string]bool
	// conns holds each link's open connections.
	conns map[string]map[net.Conn]*held
	// connects holds when the link's connect calls of the last minute were made.
	connects map[string][]time.Time
	// lastError holds why the link's last connection did not get to its worker, or, of a link without a node, that
	// its node did not start.
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
		dial:       dialWorker,
		links:      map[string]DesiredLink{},
		nodes:      map[string]Node{},
		relogin:    map[string]bool{},
		removed:    map[string]bool{},
		conns:      map[string]map[net.Conn]*held{},
		connects:   map[string][]time.Time{},
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

	// What the primary last took, and when; nil while it has to be told again.
	var posted map[string]LinkStatus
	var postedAt time.Time
	report := func() {
		g.mu.Lock()
		started := g.started
		g.mu.Unlock()
		if !started {
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
//
// A pass first does what the new rows revoke, before any call to a node, which may take 10 s: the rows are the ones
// connections are checked against from then on, and the open connections are closed of every link that is not on
// any more, or whose closed_before moved, with what they wait for given up. Then every link that stays gets its
// node, after which the status may be reported, and the removals come last.
func (g *Gateway) Reconcile(ctx context.Context) error {
	answer, err := g.primary.Links(ctx)
	if err != nil {
		return err
	}
	listed := make(map[string]DesiredLink, len(answer))
	var keeping, removing []DesiredLink
	for _, link := range answer {
		if !linkID.MatchString(link.ID) {
			log.Printf("ignoring a link whose id is not 16 hex characters: %q", link.ID)
			continue
		}
		switch link.Desired {
		case "on", "suspended":
			keeping = append(keeping, link)
		case "removing":
			removing = append(removing, link)
		default:
			log.Printf("link %s: ignoring the unknown desired state %q", link.ID, link.Desired)
			continue
		}
		listed[link.ID] = link
	}

	g.mu.Lock()
	before := g.links
	g.links = listed
	var revoked []net.Conn
	for id, open := range g.conns {
		// The primary moves closed_before to cut what is open, whenever this clock saw it opened.
		if link := listed[id]; link.Desired != "on" || link.ClosedBefore != before[id].ClosedBefore {
			for conn, kept := range open {
				kept.cancel()
				revoked = append(revoked, conn)
			}
			delete(g.conns, id)
		}
	}
	for id := range g.connects {
		if _, ok := listed[id]; !ok {
			delete(g.connects, id)
		}
	}
	for id := range g.lastError {
		// A link that is removing has no node to report an error with.
		if link, ok := listed[id]; !ok || link.Desired == "removing" {
			delete(g.lastError, id)
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

	for _, link := range keeping {
		g.keep(ctx, link, before[link.ID].LoginAfter != link.LoginAfter)
	}
	g.mu.Lock()
	g.started = true
	var unlisted []string
	for id := range g.nodes {
		if _, ok := listed[id]; !ok {
			unlisted = append(unlisted, id)
		}
	}
	g.mu.Unlock()
	for _, id := range unlisted {
		if node := g.drop(id); node != nil {
			if err := node.Close(); err != nil {
				log.Printf("link %s: closing its node: %v", id, err)
			}
		}
	}
	for _, link := range removing {
		gone := g.remove(ctx, link)
		g.mu.Lock()
		if gone {
			g.removed[link.ID] = true
		} else {
			delete(g.removed, link.ID)
		}
		g.mu.Unlock()
	}
	return nil
}

// keep runs the link's node and takes its connections, and asks a node that needs a login for a new login URL when
// it fell back from running or the link's login_after moved since the pass before.
func (g *Gateway) keep(ctx context.Context, link DesiredLink, moved bool) {
	g.mu.Lock()
	node := g.nodes[link.ID]
	owed := g.relogin[link.ID]
	g.mu.Unlock()
	if node == nil {
		// Never the hostname: two nodes on one directory overwrite each other's state.
		started, err := g.factory(filepath.Join(g.stateDir, link.ID), link.Hostname)
		g.mu.Lock()
		if err != nil {
			g.lastError[link.ID] = nodeDidNotStart
		} else {
			g.nodes[link.ID] = started
			delete(g.lastError, link.ID)
		}
		g.mu.Unlock()
		if err != nil {
			log.Printf("link %s: starting its node: %v", link.ID, err)
			return
		}
		go g.serve(ctx, link, started)
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
	for conn, kept := range open {
		kept.cancel()
		conn.Close()
	}
	return node
}

// Status returns what Run reports to the primary: every node the gateway holds as its tailnet sees it, every link
// whose node did not start as one that needs a login, with the error, and every link whose removal is done as
// removed. It does not wait for a pass under way.
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
		for _, kept := range g.conns[id] {
			if !kept.reached.IsZero() && now.Sub(kept.reached) >= countedAfter {
				link.OpenConnections++
			}
		}
		if detail, ok := g.lastError[id]; ok {
			link.LastError = &detail
		}
		status[id] = link
	}
	for id, detail := range g.lastError {
		if _, held := g.nodes[id]; !held {
			status[id] = LinkStatus{State: "needs_login", LastError: &detail}
		}
	}
	for id := range g.removed {
		status[id] = LinkStatus{State: "removed"}
	}
	return status
}
