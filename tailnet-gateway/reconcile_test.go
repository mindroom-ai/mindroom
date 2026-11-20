package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"
)

// fakeNode is a node of no tailnet: a local listener, and what the test says the tailnet sees of it.
type fakeNode struct {
	dir, hostname string
	listener      net.Listener

	mu        sync.Mutex
	observed  NodeObserved
	calls     []string
	logoutIn  time.Duration
	logoutErr error
	loginErr  error
	// reloginHeld, when set, holds every Relogin until it is closed; reloginEntered tells of each one held.
	reloginHeld    chan struct{}
	reloginEntered chan struct{}
	// logoutHeld and logoutEntered do the same for Logout.
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

func (n *fakeNode) WhoIs(context.Context, string) (string, string, error) {
	return "", "", errors.New("no tailnet")
}

func (n *fakeNode) Observed() NodeObserved {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.observed
}

func (n *fakeNode) Relogin(ctx context.Context) error {
	n.mu.Lock()
	n.calls = append(n.calls, "Relogin")
	held, entered, err := n.reloginHeld, n.reloginEntered, n.loginErr
	n.mu.Unlock()
	hold(ctx, held, entered)
	return err
}

// holdRelogin makes every Relogin of the node wait for the returned release, which the test's end calls too.
func (n *fakeNode) holdRelogin(t *testing.T) (release func()) {
	t.Helper()
	held := make(chan struct{})
	release = sync.OnceFunc(func() { close(held) })
	t.Cleanup(release)
	n.mu.Lock()
	defer n.mu.Unlock()
	n.reloginHeld, n.reloginEntered = held, make(chan struct{}, 16)
	return release
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

func (n *fakeNode) observe(seen NodeObserved) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.observed = seen
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

// fakeNodes is the factory of fake nodes: like tsnet it makes the node's directory, and a new node needs a login.
type fakeNodes struct {
	mu        sync.Mutex
	started   []*fakeNode
	startErr  error
	logoutErr error
	// logoutHeld and logoutEntered are given to every node started from now on.
	logoutHeld    chan struct{}
	logoutEntered chan struct{}
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
		observed:  NodeObserved{State: "needs_login"},
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

// settle waits until the gateway's pass and every report are over: its passes sleep until the next one, which
// they do once their report is over, and its reports until the next 5 s have passed.
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
func newFixtures(t *testing.T, enrollment string) *fixtures {
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
	f.gateway = NewGateway(NewPrimary(f.primary.URL, "tok"), f.nodes.start, f.state, enrollment)
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
func (f *fixtures) posted(t *testing.T) []map[string]any {
	t.Helper()
	var bodies []map[string]any
	for _, request := range f.primary.sent("POST /status") {
		bodies = append(bodies, jsonValue(t, request.body).(map[string]any))
	}
	return bodies
}

func on(id string) DesiredLink {
	return DesiredLink{ID: id, Hostname: "mind-bas", Desired: "on", AllowedLogin: ptr("bas@github"), LoginAfter: 100}
}

func removing(id string) DesiredLink {
	link := on(id)
	link.Desired = "removing"
	return link
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
	f.gateway.conns[id] = map[net.Conn]time.Time{ours: f.clock.Now()}
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
	f := newFixtures(t, "login")
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
	want := map[string]LinkStatus{idA: {State: "needs_login"}}
	if got := f.gateway.Status(); !reflect.DeepEqual(got, want) {
		t.Errorf("status = %+v, want %+v", got, want)
	}
}

func TestSuspendedKeepsItsNode(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	suspended := on(idA)
	suspended.Desired = "suspended"
	f.primary.list(t, suspended)

	f.reconcile(t)

	if node := f.nodes.node(t, 0); f.nodes.count() != 1 || len(node.calls) != 0 {
		t.Errorf("%d nodes started and the first got %v, want the one node untouched", f.nodes.count(), node.calls)
	}
	if got := f.gateway.Status()[idA].State; got != "needs_login" {
		t.Errorf("state = %q, want the node's needs_login", got)
	}
}

func TestUnknownDesiredStateIsALinkNotListed(t *testing.T) {
	f := newFixtures(t, "login")
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
	if got := f.gateway.Status(); len(got) != 0 {
		t.Errorf("status = %+v, want no link reported", got)
	}
}

func TestStatusReportsWhatTheNodeSees(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	seen := NodeObserved{
		State:     "running",
		LoginURL:  ptr("https://login.tailscale.com/a/1c0ffee"),
		Tailnet:   ptr("tail1234.ts.net"),
		DNSName:   ptr("mind-bas.tail1234.ts.net"),
		LoginName: ptr("bas@github"),
		KeyExpiry: ptr("2027-05-19T12:00:00Z"),
	}
	f.nodes.node(t, 0).observe(seen)

	want := map[string]LinkStatus{idA: {
		State:     "running",
		LoginURL:  seen.LoginURL,
		Tailnet:   seen.Tailnet,
		DNSName:   seen.DNSName,
		LoginName: seen.LoginName,
		KeyExpiry: seen.KeyExpiry,
	}}
	if got := f.gateway.Status(); !reflect.DeepEqual(got, want) {
		t.Errorf("status = %+v, want %+v", got, want)
	}
}

func TestStatusCountsAConnectionOnce75SecondsOld(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	f.connect(t, idA)

	f.clock.advance(74 * time.Second)
	young := f.gateway.Status()[idA].OpenConnections
	f.clock.advance(time.Second)
	old := f.gateway.Status()[idA].OpenConnections

	if young != 0 || old != 1 {
		t.Errorf("open connections = %d at 74 s and %d at 75 s, want 0 and 1", young, old)
	}
}

func TestNodeThatDoesNotStartIsTriedAgain(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.nodes.startErr = errors.New("no space left on device")

	f.reconcile(t)
	failed := f.gateway.Status()
	f.nodes.startErr = nil
	f.reconcile(t)

	if len(failed) != 0 {
		t.Errorf("status = %+v, want no node reported while none started", failed)
	}
	if got := f.gateway.Status()[idA].State; got != "needs_login" {
		t.Errorf("state = %q, want the node of the second pass", got)
	}
}

func TestUnlistedStateIsStoppedAndKept(t *testing.T) {
	f := newFixtures(t, "login")
	never := filepath.Join(f.state, "gone")
	mkdir(t, never)
	f.primary.list(t, on(idA), on(idB))
	f.reconcile(t)
	f.primary.list(t, on(idB))

	f.reconcile(t)

	unlisted, listed := f.nodes.node(t, 0), f.nodes.node(t, 1)
	if !reflect.DeepEqual(unlisted.calls, []string{"Close"}) || len(listed.calls) != 0 {
		t.Errorf("calls = %v for the unlisted node and %v for the listed, want [Close] and none", unlisted.calls, listed.calls)
	}
	if !exists(t, unlisted.dir) || !exists(t, never) {
		t.Error("a state directory the primary does not list is gone")
	}
	if _, reported := f.gateway.Status()[idA]; reported {
		t.Error("the stopped node is still reported")
	}

	// Its enrolment is kept for when the primary lists it again.
	f.primary.list(t, on(idA), on(idB))
	f.reconcile(t)
	if back := f.nodes.node(t, 2); back.dir != unlisted.dir {
		t.Errorf("the link came back in %s, want %s", back.dir, unlisted.dir)
	}
}

func TestUnlistedLinkLosesItsConnections(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	theirs := f.connect(t, idA)
	f.primary.list(t)

	f.reconcile(t)

	if !closed(t, theirs) {
		t.Error("the connection of an unlisted link is still open")
	}
}

func TestStatusCarriesALinksLastErrorWhileItIsListed(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	f.gateway.lastError[idA] = "scope"

	f.reconcile(t)
	listed := f.gateway.Status()[idA].LastError
	f.primary.list(t)
	f.reconcile(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)

	if listed == nil || *listed != "scope" {
		t.Errorf("last error = %v, want scope", listed)
	}
	if again := f.gateway.Status()[idA].LastError; again != nil {
		t.Errorf("last error = %q after the link was not listed, want none", *again)
	}
}

func TestFailedOrEmptyLinksAnswerChangesNothing(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)

	f.primary.answer("GET /links", http.StatusInternalServerError, `{"detail": "x"}`)
	err := f.gateway.Reconcile(context.Background())

	if err == nil {
		t.Error("a pass that could not read the links returned no error")
	}
	if len(node.calls) != 0 || f.gateway.Status()[idA].State != "needs_login" {
		t.Errorf("after a failed answer the node got %v and the status is %+v, want nothing changed", node.calls, f.gateway.Status())
	}

	f.primary.list(t)
	f.reconcile(t)

	if node.called("Logout") != 0 {
		t.Error("an empty answer logged the node out")
	}
	if !exists(t, node.dir) {
		t.Error("an empty answer deleted the node's state")
	}
}

func TestNullLinksAnswerLeavesALiveNodeUntouched(t *testing.T) {
	f := newFixtures(t, "login")
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
	if len(node.calls) != 0 || f.gateway.Status()[idA].State != "needs_login" {
		t.Errorf("after a null answer the node got %v and the status is %+v, want nothing changed", node.calls, f.gateway.Status())
	}
	if closed(t, theirs) {
		t.Error("a null answer closed the link's connection")
	}
}

func TestRemovingLogsOutDeletesDirAndReportsRemoved(t *testing.T) {
	f := newFixtures(t, "login")
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
	want := map[string]LinkStatus{idA: {State: "removed"}}
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
	if got := f.gateway.Status(); len(got) != 0 {
		t.Errorf("status = %+v, want nothing of a link no longer listed", got)
	}
}

func TestRemovingStartsAStoppedNodeToLogItOut(t *testing.T) {
	f := newFixtures(t, "login")
	dir := filepath.Join(f.state, idA)
	mkdir(t, dir)
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	node := f.nodes.node(t, 0)
	if node.dir != dir || !reflect.DeepEqual(node.calls, []string{"Logout", "Close"}) {
		t.Errorf("a node in %s got %v, want one in %s to get [Logout Close]", node.dir, node.calls, dir)
	}
	if exists(t, dir) || f.gateway.Status()[idA].State != "removed" {
		t.Errorf("state directory kept or status %+v, want it deleted and removed", f.gateway.Status())
	}
}

func TestRemovingWithoutStateStillReportsRemoved(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	if f.nodes.count() != 0 {
		t.Errorf("the gateway started %d nodes for a link without state, want 0", f.nodes.count())
	}
	if got := f.gateway.Status()[idA].State; got != "removed" {
		t.Errorf("state = %q, want removed", got)
	}
}

func TestRemovingDeletesEvenWhenLogoutFails(t *testing.T) {
	f := newFixtures(t, "login")
	f.nodes.logoutErr = errors.New("the control server does not answer")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	if !reflect.DeepEqual(node.calls, []string{"Logout", "Close"}) {
		t.Errorf("the node got %v, want [Logout Close]", node.calls)
	}
	if exists(t, node.dir) || f.gateway.Status()[idA].State != "removed" {
		t.Errorf("state directory kept or status %+v, want it deleted and removed", f.gateway.Status())
	}
}

func TestRemovingDeletesEvenWhenTheNodeDoesNotStart(t *testing.T) {
	f := newFixtures(t, "login")
	dir := filepath.Join(f.state, idA)
	mkdir(t, dir)
	f.nodes.startErr = errors.New("the state does not load")
	f.primary.list(t, removing(idA))

	f.reconcile(t)

	if exists(t, dir) || f.gateway.Status()[idA].State != "removed" {
		t.Errorf("state directory kept or status %+v, want it deleted and removed", f.gateway.Status())
	}
}

func TestLoginAfterChangeCallsRelogin(t *testing.T) {
	f := newFixtures(t, "login")
	link := on(idA)
	pass := func(loginAfter float64, state string) int {
		t.Helper()
		link.LoginAfter = loginAfter
		f.primary.list(t, link)
		if f.nodes.count() > 0 {
			f.nodes.node(t, 0).observe(NodeObserved{State: state})
		}
		f.reconcile(t)
		return f.nodes.node(t, 0).called("Relogin")
	}

	// A new node asks for its own login, and a value that stays calls nothing.
	pass(100, "needs_login")
	if got := pass(100, "needs_login"); got != 0 {
		t.Errorf("%d Relogin calls while login_after stayed, want 0", got)
	}
	if got := pass(200, "needs_login"); got != 1 {
		t.Errorf("%d Relogin calls after login_after moved, want 1", got)
	}
	if got := pass(200, "needs_login"); got != 1 {
		t.Errorf("%d Relogin calls after a pass without a move, want still 1", got)
	}
	// A move while the node waits for approval is no reason, not then and not later.
	if got := pass(300, "needs_approval"); got != 1 {
		t.Errorf("%d Relogin calls after a move while the node waited for approval, want still 1", got)
	}
	if got := pass(300, "needs_login"); got != 1 {
		t.Errorf("%d Relogin calls for a move of an earlier pass, want still 1", got)
	}
}

func TestNeedsLoginAfterRunningCallsReloginOnce(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	node.observe(NodeObserved{State: "running"})
	f.reconcile(t)

	node.observe(NodeObserved{State: "needs_login"})
	f.reconcile(t)
	f.reconcile(t)

	if got := node.called("Relogin"); got != 1 {
		t.Errorf("%d Relogin calls for a node that fell back to needs_login, want 1", got)
	}

	// Each fall back gets its own.
	node.observe(NodeObserved{State: "running"})
	f.reconcile(t)
	node.observe(NodeObserved{State: "needs_login"})
	f.reconcile(t)
	if got := node.called("Relogin"); got != 2 {
		t.Errorf("%d Relogin calls after a second fall back, want 2", got)
	}
}

func TestFailedReloginIsTriedAgain(t *testing.T) {
	f := newFixtures(t, "login")
	link := on(idA)
	f.primary.list(t, link)
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	node.loginErr = errors.New("the control server does not answer")
	link.LoginAfter = 200
	f.primary.list(t, link)

	f.reconcile(t)
	f.reconcile(t)
	node.loginErr = nil
	f.reconcile(t)
	f.reconcile(t)

	if got := node.called("Relogin"); got != 3 {
		t.Errorf("%d Relogin calls, want 3: two that failed and the one that worked", got)
	}
}

func TestStatusPostedOnChangeAndCarriesEnrollment(t *testing.T) {
	f := newFixtures(t, "auth_key")
	f.primary.list(t, on(idA))

	f.run(t)
	f.clock.pass(t)

	posted := f.posted(t)
	if len(posted) != 1 {
		t.Fatalf("%d status posts after two identical passes, want 1", len(posted))
	}
	if got := len(f.primary.sent("GET /links")); got != 2 {
		t.Errorf("%d passes read the links, want 2: one at start and one 5 s later", got)
	}
	want := map[string]any{
		"enrollment": "auth_key",
		"links": map[string]any{idA: map[string]any{
			"state": "needs_login", "login_url": nil, "tailnet": nil, "dns_name": nil, "login_name": nil,
			"key_expiry": nil, "open_connections": 0.0, "last_error": nil,
		}},
	}
	if !reflect.DeepEqual(posted[0], want) {
		t.Errorf("status body = %v, want %v", posted[0], want)
	}

	f.nodes.node(t, 0).observe(NodeObserved{State: "running", LoginName: ptr("bas@github")})
	f.clock.pass(t)

	posted = f.posted(t)
	if len(posted) != 2 {
		t.Fatalf("%d status posts after the node's state changed, want 2", len(posted))
	}
	link := posted[1]["links"].(map[string]any)[idA].(map[string]any)
	if link["state"] != "running" || link["login_name"] != "bas@github" {
		t.Errorf("status of the link = %v, want it running as bas@github", link)
	}
}

func TestGatewayWithoutLinksStillSaysHowItEnrols(t *testing.T) {
	f := newFixtures(t, "login")

	f.run(t)

	want := []map[string]any{{"enrollment": "login", "links": map[string]any{}}}
	if got := f.posted(t); !reflect.DeepEqual(got, want) {
		t.Errorf("status posts = %v, want %v", got, want)
	}
}

func TestUnchangedStatusIsPostedEvery30Seconds(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.run(t)

	for range 5 {
		f.clock.pass(t)
	}
	early := len(f.posted(t))
	f.clock.pass(t)

	if late := len(f.posted(t)); early != 1 || late != 2 {
		t.Errorf("%d status posts after 25 s and %d after 30 s, want 1 and 2", early, late)
	}
}

func TestStatusThePrimaryDidNotTakeIsPostedOnTheNextPass(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.answer("POST /status", http.StatusServiceUnavailable, `{"detail": "Tailnet SSH is off."}`)
	f.run(t)
	f.primary.answer("POST /status", http.StatusNoContent, "")

	f.clock.pass(t)
	f.clock.pass(t)

	if got := len(f.posted(t)); got != 2 {
		t.Errorf("%d status posts, want 2: the one refused and one on the pass after it", got)
	}
}

func TestNoStatusIsPostedBeforeTheLinksWereRead(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.answer("GET /links", http.StatusServiceUnavailable, `{"detail": "MindRoom's configuration is not loaded."}`)

	// A report without the nodes the gateway has yet to start would tell the primary they are gone.
	f.run(t)
	for range 7 {
		f.clock.pass(t)
	}
	unread := len(f.posted(t))
	f.primary.list(t, on(idA))
	f.clock.pass(t)

	if read := len(f.posted(t)); unread != 0 || read != 1 {
		t.Errorf("%d status posts in 35 s without the links and %d once they were read, want 0 and 1", unread, read)
	}
}

func TestStatusIsPostedWhileTheLinksCannotBeReadAgain(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	f.run(t)
	f.primary.answer("GET /links", http.StatusServiceUnavailable, `{"detail": "MindRoom's configuration is not loaded."}`)

	for range 6 {
		f.clock.pass(t)
	}

	posted := f.posted(t)
	if len(posted) != 2 {
		t.Fatalf("%d status posts in 30 s, want 2: the nodes it holds stay reported", len(posted))
	}
	if _, held := posted[1]["links"].(map[string]any)[idA]; !held {
		t.Errorf("status body = %v, want the node the gateway still holds", posted[1])
	}
}

func TestStatusIsPostedWhileTheFirstPassIsHeldUp(t *testing.T) {
	f := newFixtures(t, "login")
	mkdir(t, filepath.Join(f.state, idB))
	logouts, endLogout := f.nodes.holdLogouts(t)
	// The link to remove is listed first, and its logout does not end.
	f.primary.list(t, removing(idB), on(idA))
	f.start(t)
	await(t, logouts, "the logout of its first pass")
	f.clock.asleep(t, 1)

	// The other link's node is up by then, and its row is the one connections are checked against.
	healthy := f.nodes.node(t, 0)
	f.gateway.mu.Lock()
	_, known := f.gateway.links[idA]
	f.gateway.mu.Unlock()
	if healthy.dir != filepath.Join(f.state, idA) || !known {
		t.Fatalf("the first node started in %s and the link is known: %v; want the link that is on, before any logout", healthy.dir, known)
	}

	// Its status is posted 5 s after the start, long before a report would be stale.
	f.clock.advance(5 * time.Second)
	f.clock.asleep(t, 1)
	want := []map[string]any{{
		"enrollment": "login",
		"links": map[string]any{idA: map[string]any{
			"state": "needs_login", "login_url": nil, "tailnet": nil, "dns_name": nil, "login_name": nil,
			"key_expiry": nil, "open_connections": 0.0, "last_error": nil,
		}},
	}}
	if got := f.posted(t); !reflect.DeepEqual(got, want) {
		t.Fatalf("status posts while the first pass is held up = %v, want %v", got, want)
	}

	// While the next post has no answer the pass ends, with one more change, and the next 30 s pass.
	posts := f.holdPosts(t)
	healthy.observe(NodeObserved{State: "running"})
	f.clock.advance(5 * time.Second)
	await(t, posts.entered, "the post of the changed status")
	endLogout()
	f.clock.advance(30 * time.Second)
	if !quiet(posts.entered) {
		t.Error("a second POST /status was sent while the first had no answer")
	}
	posts.release()
	f.clock.settle(t)

	posted := f.posted(t)
	if len(posted) != 3 {
		t.Fatalf("%d status posts, want 3: the link that is on, its change, and the removal once it was done", len(posted))
	}
	changed, done := posted[1]["links"].(map[string]any), posted[2]["links"].(map[string]any)
	if changed[idA].(map[string]any)["state"] != "running" || changed[idB] != nil {
		t.Errorf("second status post = %v, want the link that is on as running, and nothing of the other", changed)
	}
	if done[idA].(map[string]any)["state"] != "running" || done[idB].(map[string]any)["state"] != "removed" {
		t.Errorf("third status post = %v, want one link running and the other removed", done)
	}
	if posts.atOnce() != 1 {
		t.Errorf("%d POST /status were in flight at once, want 1", posts.atOnce())
	}
}

func TestStatusIsPostedWhileAPassIsHeldUp(t *testing.T) {
	f := newFixtures(t, "login")
	link := on(idA)
	f.primary.list(t, link)
	f.run(t)
	node := f.nodes.node(t, 0)
	endPass := node.holdRelogin(t)
	link.LoginAfter = 200
	f.primary.list(t, link)
	f.clock.advance(5 * time.Second)
	await(t, node.reloginEntered, "the pass that asks the node for a new login")
	// The loop that reports sleeps again; the passes do not, held up as they are.
	f.clock.asleep(t, 1)
	posts := f.holdPosts(t)

	// The pass is 25 s old and not over when 30 s have passed since the last report.
	f.clock.advance(25 * time.Second)
	await(t, posts.entered, "a status post while its pass is held up")

	// While that post has no answer the pass ends, with a changed status, and the next 30 s pass.
	node.observe(NodeObserved{State: "running"})
	endPass()
	f.clock.advance(30 * time.Second)
	if !quiet(posts.entered) {
		t.Error("a second POST /status was sent while the first had no answer")
	}
	posts.release()
	f.clock.settle(t)

	posted := f.posted(t)
	if len(posted) != 3 {
		t.Fatalf("%d status posts, want 3: at the start, 30 s later, and after the pass that changed it", len(posted))
	}
	for i, state := range []string{"needs_login", "needs_login", "running"} {
		if got := posted[i]["links"].(map[string]any)[idA].(map[string]any)["state"]; got != state {
			t.Errorf("status post %d says %v, want %s", i+1, got, state)
		}
	}
	if posts.atOnce() != 1 {
		t.Errorf("%d POST /status were in flight at once, want 1", posts.atOnce())
	}
}

func TestStatusPostsNeverOverlap(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	posts := f.holdPosts(t)
	f.start(t)
	await(t, posts.entered, "its first status post")

	// While the first post waits for its answer the status changes, and both of the loop's periods pass, twice.
	f.nodes.node(t, 0).observe(NodeObserved{State: "running"})
	for range 12 {
		f.clock.advance(5 * time.Second)
	}
	if !quiet(posts.entered) {
		t.Error("a second POST /status was sent while the first had no answer")
	}
	posts.release()
	f.clock.settle(t)
	f.clock.pass(t)

	if got := len(f.posted(t)); got != 2 {
		t.Errorf("%d status posts, want 2: the first, and the changed status once it had its answer", got)
	}
	if posts.atOnce() != 1 {
		t.Errorf("%d POST /status were in flight at once, want 1", posts.atOnce())
	}
}

func TestInvalidLinkIDIsIgnored(t *testing.T) {
	f := newFixtures(t, "login")
	outside := filepath.Join(filepath.Dir(f.state), "x")
	mkdir(t, outside)
	mkdir(t, filepath.Join(f.state, "0123456789ABCDEF"))
	escaping, upper, short := removing("../x"), removing("0123456789ABCDEF"), on("0123456789abcde")
	f.primary.list(t, escaping, upper, short, on(""), on("."), on(idA+"/../"+idB))

	f.reconcile(t)

	if f.nodes.count() != 0 {
		t.Errorf("the gateway started %d nodes for ids that are not 16 hex characters, want 0", f.nodes.count())
	}
	if !exists(t, outside) || !exists(t, filepath.Join(f.state, "0123456789ABCDEF")) {
		t.Error("a link with an invalid id deleted a directory")
	}
	entries, err := os.ReadDir(f.state)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Errorf("the state directory holds %d entries, want only the one made before the pass", len(entries))
	}
	if got := f.gateway.Status(); len(got) != 0 {
		t.Errorf("status = %+v, want no link reported", got)
	}
}
