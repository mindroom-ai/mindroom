package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"
)

// fakeNode is a node of no tailnet: a local listener, and what the test says the tailnet sees of it.
type fakeNode struct {
	dir, hostname string
	listener      net.Listener

	mu sync.Mutex
	// peer is who the tailnet says is behind every connection; nil is a peer it does not know.
	peer      *fakePeer
	calls     []string
	logoutIn  time.Duration
	logoutErr error
	// logoutHeld delays Logout; logoutEntered signals that the call reached it.
	logoutHeld    chan struct{}
	logoutEntered chan struct{}
}

// hold tells entered of a call and keeps it until held is closed or the call's context ends; without held it
// returns at once.
func hold(ctx context.Context, held, entered chan struct{}) {
	if held == nil {
		return
	}
	entered <- struct{}{}
	select {
	case <-held:
	case <-ctx.Done():
	}
}

func (n *fakeNode) Listener() net.Listener { return n.listener }

// fakePeer is a tailnet peer: its login, which is "" for a tagged device, and its machine.
type fakePeer struct{ login, node string }

func (n *fakeNode) WhoIs(context.Context, string) (string, string, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.peer == nil {
		return "", "", errors.New("no tailnet")
	}
	return n.peer.login, n.peer.node, nil
}

// peerIs sets who the tailnet says is behind every connection to the node.
func (n *fakeNode) peerIs(login, node string) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.peer = &fakePeer{login, node}
}

func (n *fakeNode) Logout(ctx context.Context) error {
	n.mu.Lock()
	n.calls = append(n.calls, "Logout")
	if deadline, ok := ctx.Deadline(); ok {
		n.logoutIn = time.Until(deadline).Round(time.Second)
	}
	held, entered, err := n.logoutHeld, n.logoutEntered, n.logoutErr
	n.mu.Unlock()
	hold(ctx, held, entered)
	return err
}

func (n *fakeNode) Close() error {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.calls = append(n.calls, "Close")
	return n.listener.Close()
}

// called returns how often the gateway called the method.
func (n *fakeNode) called(method string) int {
	n.mu.Lock()
	defer n.mu.Unlock()
	count := 0
	for _, call := range n.calls {
		if call == method {
			count++
		}
	}
	return count
}

// fakeNodes is the factory of fake nodes: like tsnet it makes the node's directory.
type fakeNodes struct {
	mu        sync.Mutex
	started   []*fakeNode
	startErr  error
	logoutErr error
	// logoutHeld and logoutEntered are given to every node started from now on.
	logoutHeld    chan struct{}
	logoutEntered chan struct{}
	// startHeld, when set, holds every start until endStarts closes it; startEntered tells of each one held.
	startHeld    chan struct{}
	startEntered chan struct{}
	endStarts    func()
}

// holdStarts makes every start of a node wait for the returned release, which the end of the gateway's loop calls
// too; entered tells of each one that waits.
func (f *fakeNodes) holdStarts() (entered <-chan struct{}, release func()) {
	held := make(chan struct{})
	f.mu.Lock()
	defer f.mu.Unlock()
	f.startHeld, f.startEntered, f.endStarts = held, make(chan struct{}, 16), sync.OnceFunc(func() { close(held) })
	return f.startEntered, f.endStarts
}

// holdLogouts makes the Logout of every node started from now on wait for the returned release, which the test's
// end calls too; entered tells of each one that waits.
func (f *fakeNodes) holdLogouts(t *testing.T) (entered <-chan struct{}, release func()) {
	t.Helper()
	held := make(chan struct{})
	release = sync.OnceFunc(func() { close(held) })
	t.Cleanup(release)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.logoutHeld, f.logoutEntered = held, make(chan struct{}, 16)
	return f.logoutEntered, release
}

func (f *fakeNodes) start(dir, hostname string) (Node, error) {
	f.mu.Lock()
	held, entered := f.startHeld, f.startEntered
	f.mu.Unlock()
	hold(context.Background(), held, entered)
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.startErr != nil {
		return nil, f.startErr
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	node := &fakeNode{
		dir:       dir,
		hostname:  hostname,
		listener:  listener,
		logoutErr: f.logoutErr,

		logoutHeld:    f.logoutHeld,
		logoutEntered: f.logoutEntered,
	}
	f.started = append(f.started, node)
	return node, nil
}

// node returns the i-th node the gateway started, failing the test when it started fewer.
func (f *fakeNodes) node(t *testing.T, i int) *fakeNode {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.started) <= i {
		t.Fatalf("the gateway started %d nodes, want more than %d", len(f.started), i)
	}
	return f.started[i]
}

func (f *fakeNodes) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.started)
}

// fakeClock is a clock only the test moves.
type fakeClock struct {
	mu       sync.Mutex
	now      time.Time
	sleepers []sleeper
}

type sleeper struct {
	until time.Time
	wake  chan time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) After(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	wake := make(chan time.Time, 1)
	c.sleepers = append(c.sleepers, sleeper{c.now.Add(d), wake})
	return wake
}

// advance moves the clock and wakes every sleeper whose time has come.
func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	var asleep []sleeper
	for _, s := range c.sleepers {
		if s.until.After(c.now) {
			asleep = append(asleep, s)
		} else {
			s.wake <- c.now
		}
	}
	c.sleepers = asleep
}

// asleep waits until so many sleepers wait for the clock.
func (c *fakeClock) asleep(t *testing.T, sleepers int) {
	t.Helper()
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(time.Millisecond) {
		c.mu.Lock()
		asleep := len(c.sleepers)
		c.mu.Unlock()
		if asleep >= sleepers {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d of the gateway's loops sleep, want %d", asleep, sleepers)
		}
	}
}

// settle waits until both loops have registered the next timers.
func (c *fakeClock) settle(t *testing.T) {
	t.Helper()
	c.asleep(t, 2)
}

// pass lets the loop's next pass come and waits for its end.
func (c *fakeClock) pass(t *testing.T) {
	t.Helper()
	c.advance(5 * time.Second)
	c.settle(t)
}

type fixtures struct {
	primary *fakePrimary
	nodes   *fakeNodes
	state   string
	gateway *Gateway
	clock   *fakeClock
}

// newFixtures is a gateway on a fake primary, fake nodes, a fake clock and an empty state directory.
func newFixtures(t *testing.T) *fixtures {
	t.Helper()
	f := &fixtures{
		primary: newFakePrimary(t),
		nodes:   &fakeNodes{},
		state:   filepath.Join(t.TempDir(), "state"),
		clock:   &fakeClock{now: time.Unix(1795000000, 0)},
	}
	if err := os.Mkdir(f.state, 0o700); err != nil {
		t.Fatal(err)
	}
	f.gateway = NewGateway(NewPrimary(f.primary.URL, "tok"), f.nodes.start, f.state)
	f.gateway.clock = f.clock
	return f
}

func (f *fixtures) reconcile(t *testing.T) {
	t.Helper()
	if err := f.gateway.Reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// start starts the gateway's loop, and ends it with the test.
func (f *fixtures) start(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		f.gateway.Run(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		// A start takes no context, so one still held would hold the loop's end.
		f.nodes.mu.Lock()
		endStarts := f.nodes.endStarts
		f.nodes.mu.Unlock()
		if endStarts != nil {
			endStarts()
		}
		<-done
	})
}

// run starts the gateway's loop and returns after its first pass.
func (f *fixtures) run(t *testing.T) {
	t.Helper()
	f.start(t)
	f.clock.settle(t)
}

// heldPosts holds every POST /status at the fake primary, and knows how many waited for an answer at once.
type heldPosts struct {
	// entered tells of each post that reached the primary.
	entered chan struct{}
	// release answers the held posts, and every later one at once.
	release func()

	mu             sync.Mutex
	inFlight, most int
}

// holdPosts holds every POST /status from now on until the returned release, which the test's end calls too.
func (f *fixtures) holdPosts(t *testing.T) *heldPosts {
	t.Helper()
	held := make(chan struct{})
	posts := &heldPosts{entered: make(chan struct{}, 16), release: sync.OnceFunc(func() { close(held) })}
	t.Cleanup(posts.release)
	f.primary.holdStatus(func() {
		posts.mu.Lock()
		posts.inFlight++
		posts.most = max(posts.most, posts.inFlight)
		posts.mu.Unlock()
		posts.entered <- struct{}{}
		<-held
		posts.mu.Lock()
		posts.inFlight--
		posts.mu.Unlock()
	})
	return posts
}

// atOnce returns the most posts that waited for an answer at the same time.
func (p *heldPosts) atOnce() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.most
}

// await waits for what the gateway is to do next, and fails the test when it does not.
func await(t *testing.T, happened <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-happened:
	case <-time.After(5 * time.Second):
		t.Fatalf("the gateway did not get to %s", what)
	}
}

// quiet says that nothing more happened within a tenth of a second.
func quiet(happened <-chan struct{}) bool {
	select {
	case <-happened:
		return false
	case <-time.After(100 * time.Millisecond):
		return true
	}
}

// posted returns the POST /status bodies the primary got, as JSON values.
func (f *fixtures) posted(t *testing.T) []StatusReport {
	t.Helper()
	var bodies []StatusReport
	for _, request := range f.primary.sent("POST /status") {
		var report StatusReport
		if err := json.Unmarshal(request.body, &report); err != nil {
			t.Fatal(err)
		}
		bodies = append(bodies, report)
	}
	return bodies
}

func (f *fixtures) wasRemoved(id string) bool { return slices.Contains(f.gateway.Status().Removed, id) }

func (f *fixtures) waitPosts(t *testing.T, count int) {
	t.Helper()
	eventually(t, "publish status", func() bool { return len(f.posted(t)) >= count })
}

func on(id string) DesiredLink {
	return DesiredLink{ID: id, Hostname: "mind-bas", Desired: "on", AllowedLogin: ptr("bas@github"), AccessRevision: "revision-a"}
}

func removing(id string) DesiredLink {
	return DesiredLink{ID: id, Hostname: "mind-bas", Desired: "removing"}
}

func exists(t *testing.T, path string) bool {
	t.Helper()
	_, err := os.Lstat(path)
	if err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	return err == nil
}

// connect gives the link an open connection, as an accepted one is kept, and returns the peer's end of it.
func (f *fixtures) connect(t *testing.T, id string) net.Conn {
	t.Helper()
	ours, theirs := net.Pipe()
	t.Cleanup(func() {
		ours.Close()
		theirs.Close()
	})
	f.gateway.mu.Lock()
	defer f.gateway.mu.Unlock()
	if f.gateway.conns[id] == nil {
		f.gateway.conns[id] = map[net.Conn]*held{}
	}
	f.gateway.conns[id][ours] = &held{cancel: func() {}, reached: f.clock.Now()}
	return theirs
}

// closed says whether the gateway closed its end of the connection.
func closed(t *testing.T, theirs net.Conn) bool {
	t.Helper()
	read := make(chan error, 1)
	go func() {
		_, err := theirs.Read(make([]byte, 1))
		read <- err
	}()
	select {
	case err := <-read:
		return errors.Is(err, io.EOF)
	case <-time.After(time.Second):
		return false
	}
}

func mkdir(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(path, 0o700); err != nil {
		t.Fatal(err)
	}
}

func TestOnStartsNodeInDirNamedByID(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))

	f.reconcile(t)
	f.reconcile(t)

	if f.nodes.count() != 1 {
		t.Fatalf("the gateway started %d nodes in two passes, want 1", f.nodes.count())
	}
	node := f.nodes.node(t, 0)
	if node.dir != filepath.Join(f.state, idA) || node.hostname != "mind-bas" {
		t.Errorf("node started in %s as %s, want %s as mind-bas", node.dir, node.hostname, filepath.Join(f.state, idA))
	}
	if exists(t, filepath.Join(f.state, "mind-bas")) {
		t.Error("the hostname names a state directory")
	}
}

func TestUnknownDesiredStateIsALinkNotListed(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	off := on(idA)
	off.Desired = "off"
	f.primary.list(t, off)

	f.reconcile(t)

	if !reflect.DeepEqual(node.calls, []string{"Close"}) || !exists(t, node.dir) {
		t.Errorf("the node got %v, want it closed and its state kept", node.calls)
	}
	if got := f.gateway.Status(); len(got.Removed) != 0 || len(got.OpenConnections) != 0 {
		t.Errorf("status = %+v, want no link reported", got)
	}
}

func TestStatusCountsAConnectionOnce75SecondsOld(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)
	f.connect(t, idA)

	f.clock.advance(74 * time.Second)
	young := f.gateway.Status().OpenConnections[idA]
	f.clock.advance(time.Second)
	old := f.gateway.Status().OpenConnections[idA]

	if young != 0 || old != 1 {
		t.Errorf("open connections = %d at 74 s and %d at 75 s, want 0 and 1", young, old)
	}
}

func TestNodeThatDoesNotStartIsTriedAgain(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.nodes.startErr = errors.New("no space left on device")
	f.reconcile(t)
	if f.nodes.count() != 0 {
		t.Fatal("a failed start left a node running")
	}
	f.nodes.startErr = nil
	f.reconcile(t)
	if f.nodes.count() != 1 {
		t.Fatal("the next pass did not retry the start")
	}
}

func TestUnlistedStateIsStoppedAndKept(t *testing.T) {
	f := newFixtures(t)
	never := filepath.Join(f.state, "gone")
	mkdir(t, never)
	f.primary.list(t, on(idA), on(idB))
	f.reconcile(t)
	f.primary.list(t, on(idB))

	f.reconcile(t)

	unlisted, listed := f.nodes.node(t, 0), f.nodes.node(t, 1)
	if filepath.Base(unlisted.dir) != idA {
		unlisted, listed = listed, unlisted
	}
	if !reflect.DeepEqual(unlisted.calls, []string{"Close"}) || len(listed.calls) != 0 {
		t.Errorf("calls = %v for the unlisted node and %v for the listed, want [Close] and none", unlisted.calls, listed.calls)
	}
	if !exists(t, unlisted.dir) || !exists(t, never) {
		t.Error("a state directory the primary does not list is gone")
	}

	// Its enrolment is kept for when the primary lists it again.
	f.primary.list(t, on(idA), on(idB))
	f.reconcile(t)
	if back := f.nodes.node(t, 2); back.dir != unlisted.dir {
		t.Errorf("the link came back in %s, want %s", back.dir, unlisted.dir)
	}
}

func TestRevokedConnectionsAreClosedBeforeAnyNodeCall(t *testing.T) {
	f := newFixtures(t)
	stay := on("123456789abcdef0")
	f.primary.list(t, on(idA), on(idB), on(idC), stay)
	f.run(t)
	removed, unlisted, cut := f.connect(t, idA), f.connect(t, idB), f.connect(t, idC)
	f.connect(t, stay.ID)
	starts, release := f.nodes.holdStarts()
	revised := on(idC)
	revised.AccessRevision = "revision-b"
	f.primary.list(t, on("123456789abcdef1"), removing(idA), revised, stay)
	f.clock.advance(reconcileEvery)
	await(t, starts, "a held node start")

	for _, conn := range []net.Conn{removed, unlisted, cut} {
		if !closed(t, conn) {
			t.Error("a revoked connection remained open during a slow node start")
		}
	}
	f.gateway.mu.Lock()
	open := len(f.gateway.conns[stay.ID])
	f.gateway.mu.Unlock()
	if open != 1 || f.wasRemoved(idA) {
		t.Fatal("kept pipe was revoked or retirement was acknowledged early")
	}
	release()
	f.clock.settle(t)
	if !f.wasRemoved(idA) {
		t.Fatal("retirement was not acknowledged")
	}
}

func TestUnlistedLinkLosesItsConnections(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)
	theirs := f.connect(t, idA)
	f.primary.list(t)

	f.reconcile(t)

	if !closed(t, theirs) {
		t.Error("the connection of an unlisted link is still open")
	}
}

func TestFailedLinksPreserveNodeButEmptyListStopsIt(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)

	f.primary.answer("GET /links", http.StatusInternalServerError, `{"detail": "x"}`)
	err := f.gateway.Reconcile(context.Background())

	if err == nil {
		t.Error("a pass that could not read the links returned no error")
	}
	if len(node.calls) != 0 {
		t.Errorf("after a failed answer the node got %v and the status is %+v, want nothing changed", node.calls, f.gateway.Status())
	}

	f.primary.list(t)
	f.reconcile(t)

	if !reflect.DeepEqual(node.calls, []string{"Close"}) {
		t.Errorf("an empty list did %v; want only Close", node.calls)
	}
	if !exists(t, node.dir) {
		t.Error("an empty answer deleted the node's state")
	}
}

func TestNullLinksAnswerLeavesALiveNodeUntouched(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	theirs := f.connect(t, idA)

	// JSON null is no list of links: read as an empty one it would stop every node.
	f.primary.answer("GET /links", http.StatusOK, "null")
	err := f.gateway.Reconcile(context.Background())

	if err == nil {
		t.Error("a pass whose links answer was null returned no error")
	}
	if len(node.calls) != 0 {
		t.Errorf("after a null answer the node got %v and the status is %+v, want nothing changed", node.calls, f.gateway.Status())
	}
	if closed(t, theirs) {
		t.Error("a null answer closed the link's connection")
	}
}

func TestRemovingLogsOutDeletesDirAndReportsRemoved(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	theirs := f.connect(t, idA)
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	if !reflect.DeepEqual(node.calls, []string{"Logout", "Close"}) {
		t.Errorf("the node got %v, want [Logout Close]", node.calls)
	}
	if node.logoutIn != 10*time.Second {
		t.Errorf("Logout may take %v, want 10s", node.logoutIn)
	}
	if exists(t, node.dir) {
		t.Error("the node's state is still there")
	}
	if !closed(t, theirs) {
		t.Error("the connection of a removed link is still open")
	}
	want := StatusReport{OpenConnections: map[string]int{}, Removed: []string{idA}}
	if got := f.gateway.Status(); !reflect.DeepEqual(got, want) {
		t.Errorf("status = %+v, want %+v", got, want)
	}

	// It stays removed while the primary lists it, and is forgotten once it does not.
	f.reconcile(t)
	if got := f.gateway.Status(); !reflect.DeepEqual(got, want) || f.nodes.count() != 1 {
		t.Errorf("status = %+v after %d nodes, want %+v and no second node", got, f.nodes.count(), want)
	}
	f.primary.list(t)
	f.reconcile(t)
	if got := f.gateway.Status(); len(got.Removed) != 0 || len(got.OpenConnections) != 0 {
		t.Errorf("status = %+v, want nothing of a link no longer listed", got)
	}
}

func TestRemovingStartsAStoppedNodeToLogItOut(t *testing.T) {
	f := newFixtures(t)
	dir := filepath.Join(f.state, idA)
	mkdir(t, dir)
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	node := f.nodes.node(t, 0)
	if node.dir != dir || !reflect.DeepEqual(node.calls, []string{"Logout", "Close"}) {
		t.Errorf("a node in %s got %v, want one in %s to get [Logout Close]", node.dir, node.calls, dir)
	}
	if exists(t, dir) || !f.wasRemoved(idA) {
		t.Errorf("state directory kept or status %+v, want it deleted and removed", f.gateway.Status())
	}
}

func TestRemovingWithoutStateStillReportsRemoved(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	if f.nodes.count() != 0 {
		t.Errorf("the gateway started %d nodes for a link without state, want 0", f.nodes.count())
	}
	if !f.wasRemoved(idA) {
		t.Error("missing removal acknowledgement")
	}
}

func TestRemovingDeletesEvenWhenLogoutFails(t *testing.T) {
	f := newFixtures(t)
	f.nodes.logoutErr = errors.New("the control server does not answer")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	if !reflect.DeepEqual(node.calls, []string{"Logout", "Close"}) {
		t.Errorf("the node got %v, want [Logout Close]", node.calls)
	}
	if exists(t, node.dir) || !f.wasRemoved(idA) {
		t.Errorf("state directory kept or status %+v, want it deleted and removed", f.gateway.Status())
	}
}

func TestRemovingDeletesEvenWhenTheNodeDoesNotStart(t *testing.T) {
	f := newFixtures(t)
	dir := filepath.Join(f.state, idA)
	mkdir(t, dir)
	f.nodes.startErr = errors.New("the state does not load")
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	if exists(t, dir) || !f.wasRemoved(idA) {
		t.Errorf("state directory kept or status %+v, want it deleted and removed", f.gateway.Status())
	}
}

func TestStatusPostedOnChange(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.run(t)
	f.waitPosts(t, 1)
	f.clock.pass(t)
	if got := len(f.primary.sent("GET /links")); got != 2 || len(f.posted(t)) != 1 {
		t.Fatalf("got %d reconciles and %d posts, want 2 and 1", got, len(f.posted(t)))
	}
	f.primary.list(t, removing(idA))
	f.clock.pass(t)
	f.waitPosts(t, 2)
	if got := f.posted(t); !slices.Equal(got[1].Removed, []string{idA}) {
		t.Fatalf("status posts = %+v; want a removal acknowledgement", got)
	}
}

func TestUnchangedStatusIsPostedEvery30Seconds(t *testing.T) {
	f := newFixtures(t)
	f.run(t)
	f.waitPosts(t, 1)
	for range 5 {
		f.clock.pass(t)
	}
	if got := len(f.posted(t)); got != 1 {
		t.Errorf("%d posts after 25 s, want 1", got)
	}
	f.clock.pass(t)
	f.waitPosts(t, 2)
}

func TestStatusThePrimaryDidNotTakeIsRetried(t *testing.T) {
	f := newFixtures(t)
	f.primary.answer("POST /status", http.StatusServiceUnavailable, `{"detail": "Tailnet SSH is disabled."}`)
	f.run(t)
	f.waitPosts(t, 1)
	f.primary.answer("POST /status", http.StatusNoContent, "")
	f.clock.pass(t)
	f.waitPosts(t, 2)
}

func TestStatusIsIndependentOfNodeStartup(t *testing.T) {
	f := newFixtures(t)
	starts, _ := f.nodes.holdStarts()
	f.primary.list(t, on(idA))
	f.start(t)
	await(t, starts, "the first node start")
	f.clock.asleep(t, 1)
	f.clock.advance(reconcileEvery)
	f.waitPosts(t, 1)
	if got := f.posted(t)[0]; len(got.Removed) != 0 || len(got.OpenConnections) != 0 {
		t.Fatalf("status during the first startup = %+v, want no activity or acknowledgement", got)
	}
}

func TestStatusIsPostedWhileTheLinksCannotBeReadAgain(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.run(t)
	f.waitPosts(t, 1)
	f.connect(t, idA)
	f.primary.answer("GET /links", http.StatusServiceUnavailable, `{"detail": "MindRoom's configuration is not loaded."}`)
	f.clock.advance(countedAfter)
	f.waitPosts(t, 2)
	if got := f.posted(t)[1].OpenConnections[idA]; got != 1 {
		t.Fatalf("status counts %d connections, want the one that stayed open", got)
	}
}

func TestStatusIsPostedWhileTheFirstPassIsHeldUp(t *testing.T) {
	f := newFixtures(t)
	mkdir(t, filepath.Join(f.state, idB))
	logouts, release := f.nodes.holdLogouts(t)
	f.primary.list(t, removing(idB), on(idA))
	f.start(t)
	await(t, logouts, "the first logout")
	if node := f.nodes.node(t, 0); filepath.Base(node.dir) != idA {
		t.Fatal("the healthy node did not start before logout")
	}
	f.clock.asleep(t, 1)
	f.clock.advance(reconcileEvery)
	f.waitPosts(t, 1)
	if len(f.posted(t)[0].Removed) != 0 {
		t.Fatal("a held logout was acknowledged")
	}
	release()
	f.waitPosts(t, 2)
	if !slices.Equal(f.posted(t)[1].Removed, []string{idB}) {
		t.Fatal("the completed retirement was not posted")
	}
}

func TestStatusIsPostedWhileAPassIsHeldUp(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	f.run(t)
	f.waitPosts(t, 1)
	starts, _ := f.nodes.holdStarts()
	f.primary.list(t, on(idA), on(idB))
	f.clock.advance(reconcileEvery)
	await(t, starts, "a held node start")
	f.clock.asleep(t, 1)
	f.clock.advance(statusEvery - reconcileEvery)
	f.waitPosts(t, 2)
	if len(f.primary.sent("GET /links")) != 2 {
		t.Fatal("there was more than one reconcile in flight")
	}
}

func TestStatusPostsNeverOverlap(t *testing.T) {
	f := newFixtures(t)
	f.primary.list(t, on(idA))
	posts := f.holdPosts(t)
	f.start(t)
	await(t, posts.entered, "the first held status post")
	f.clock.asleep(t, 2)
	f.primary.list(t, removing(idA))
	f.clock.advance(statusEvery)
	eventually(t, "complete removal while a post is held", func() bool { return f.wasRemoved(idA) })
	if !quiet(posts.entered) {
		t.Fatal("status posts overlapped")
	}
	posts.release()
	f.waitPosts(t, 2)
	if posts.atOnce() != 1 || !slices.Equal(f.posted(t)[1].Removed, []string{idA}) {
		t.Fatal("the publisher missed an acknowledgement or overlapped posts")
	}
}

func TestInvalidLinkIDIsIgnored(t *testing.T) {
	f := newFixtures(t)
	outside := filepath.Join(filepath.Dir(f.state), "x")
	mkdir(t, outside)
	mkdir(t, filepath.Join(f.state, "0123456789ABCDEF"))
	escaping, upper, short := removing("../x"), removing("0123456789ABCDEF"), on("0123456789abcde")
	f.primary.list(t, escaping, upper, short, on(""), on("."), on(idA+"/../"+idB), on(idC))

	f.reconcile(t)

	if f.nodes.count() != 1 || filepath.Base(f.nodes.node(t, 0).dir) != idC {
		t.Fatal("only the valid row should start a node")
	}
	if !exists(t, outside) || !exists(t, filepath.Join(f.state, "0123456789ABCDEF")) {
		t.Error("a link with an invalid id deleted a directory")
	}
	entries, err := os.ReadDir(f.state)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Errorf("the state directory holds %d entries, want the valid node and the one created before the pass", len(entries))
	}
	if got := f.gateway.Status(); len(got.Removed) != 0 || len(got.OpenConnections) != 0 {
		t.Errorf("status = %+v, want no link reported", got)
	}
}
