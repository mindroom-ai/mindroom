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

func (n *fakeNode) Relogin(context.Context) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.calls = append(n.calls, "Relogin")
	return n.loginErr
}

func (n *fakeNode) Logout(ctx context.Context) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.calls = append(n.calls, "Logout")
	if deadline, ok := ctx.Deadline(); ok {
		n.logoutIn = time.Until(deadline).Round(time.Second)
	}
	return n.logoutErr
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

// settle waits until the gateway's loop sleeps, which is when its pass is over.
func (c *fakeClock) settle(t *testing.T) {
	t.Helper()
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(time.Millisecond) {
		c.mu.Lock()
		asleep := len(c.sleepers)
		c.mu.Unlock()
		if asleep > 0 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("the gateway's loop did not finish its pass")
		}
	}
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

func TestStatusIsPostedWhileTheLinksCannotBeRead(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.answer("GET /links", http.StatusServiceUnavailable, `{"detail": "MindRoom's configuration is not loaded."}`)

	f.run(t)

	if got := len(f.posted(t)); got != 1 {
		t.Errorf("%d status posts after a pass that read no links, want 1", got)
	}
}

func TestStatusPostsNeverOverlap(t *testing.T) {
	f := newFixtures(t, "login")
	f.primary.list(t, on(idA))
	var mu sync.Mutex
	inFlight, most := 0, 0
	entered := make(chan struct{}, 16)
	held := make(chan struct{})
	release := sync.OnceFunc(func() { close(held) })
	f.primary.holdStatus(func() {
		mu.Lock()
		inFlight++
		most = max(most, inFlight)
		mu.Unlock()
		entered <- struct{}{}
		<-held
		mu.Lock()
		inFlight--
		mu.Unlock()
	})
	f.start(t)
	// Registered after the loop's end, so it runs before it: a held answer would hold the loop and the fake primary.
	t.Cleanup(release)
	<-entered

	// While the first post waits for its answer the status changes, and both of the loop's periods pass, twice.
	f.nodes.node(t, 0).observe(NodeObserved{State: "running"})
	for range 12 {
		f.clock.advance(5 * time.Second)
	}
	select {
	case <-entered:
		t.Error("a second POST /status was sent while the first had no answer")
	case <-time.After(100 * time.Millisecond):
	}
	release()
	f.clock.settle(t)
	f.clock.pass(t)

	if got := len(f.posted(t)); got != 2 {
		t.Errorf("%d status posts, want 2: the first, and the changed status on the pass after it", got)
	}
	mu.Lock()
	defer mu.Unlock()
	if most != 1 {
		t.Errorf("%d POST /status were in flight at once, want 1", most)
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
