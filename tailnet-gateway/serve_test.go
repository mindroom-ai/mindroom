package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"reflect"
	"sync"
	"testing"
	"time"
)

const (
	banner       = "SSH-2.0-test\r\n"
	waking       = "MindRoom: waking worker\r\n"
	connectRoute = "POST /links/" + idA + "/connect"
)

// fakeWorker is a worker's sshd as the gateway reaches it: it greets every connection with its SSH banner.
type fakeWorker struct {
	listener net.Listener
	// accepted hands out the worker's end of each connection, before it is greeted.
	accepted chan net.Conn

	mu sync.Mutex
	// silent, when set, holds every greeting until it is closed.
	silent chan struct{}
}

func newFakeWorker(t *testing.T) *fakeWorker {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	w := &fakeWorker{listener: listener, accepted: make(chan net.Conn, 16)}
	t.Cleanup(func() { listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			t.Cleanup(func() { conn.Close() })
			w.accepted <- conn
			w.mu.Lock()
			silent := w.silent
			w.mu.Unlock()
			go func() {
				if silent != nil {
					<-silent
				}
				fmt.Fprint(conn, banner)
			}()
		}
	}()
	return w
}

// holdGreetings makes the worker say nothing to its connections until the returned greet, which the test's end
// calls too.
func (w *fakeWorker) holdGreetings(t *testing.T) (greet func()) {
	t.Helper()
	silent := make(chan struct{})
	greet = sync.OnceFunc(func() { close(silent) })
	t.Cleanup(greet)
	w.mu.Lock()
	defer w.mu.Unlock()
	w.silent = silent
	return greet
}

// conn returns the worker's end of the next connection the gateway opened to it.
func (w *fakeWorker) conn(t *testing.T) net.Conn {
	t.Helper()
	select {
	case conn := <-w.accepted:
		return conn
	case <-time.After(5 * time.Second):
		t.Fatal("the gateway opened no connection to the worker")
		return nil
	}
}

// wake makes the primary send the connections of the first link to a new worker.
func (f *fixtures) wake(t *testing.T) *fakeWorker {
	t.Helper()
	w := newFakeWorker(t)
	host, port, err := net.SplitHostPort(w.listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	f.primary.answer(connectRoute, http.StatusOK, fmt.Sprintf(`{"host": %q, "port": %s}`, host, port))
	return w
}

// serving lists the link, runs one pass, and returns the link's node, whose tailnet says every peer is the link's
// login.
func (f *fixtures) serving(t *testing.T, link Link) *fakeNode {
	t.Helper()
	f.primary.list(t, link)
	f.reconcile(t)
	node := f.nodes.node(t, 0)
	node.peerIs("bas@github", "laptop")
	return node
}

// connects returns how many connect calls of the first link the primary got.
func (f *fixtures) connects() int {
	return len(f.primary.sent(connectRoute))
}

// heldConnects holds every connect call at the fake primary.
type heldConnects struct {
	// entered tells of each call that waits for its answer.
	entered chan struct{}
	// abandoned tells of each call the gateway gave up before its answer.
	abandoned chan struct{}
	// answer lets the held calls, and every later one, be answered.
	answer func()
}

// holdConnects holds every connect call at the primary until the returned answer, which the test's end calls too.
func (f *fixtures) holdConnects(t *testing.T) *heldConnects {
	t.Helper()
	held := make(chan struct{})
	calls := &heldConnects{
		entered:   make(chan struct{}, 16),
		abandoned: make(chan struct{}, 16),
		answer:    sync.OnceFunc(func() { close(held) }),
	}
	t.Cleanup(calls.answer)
	f.primary.mu.Lock()
	defer f.primary.mu.Unlock()
	f.primary.onConnect = func(r *http.Request) {
		calls.entered <- struct{}{}
		select {
		case <-held:
		case <-r.Context().Done():
			calls.abandoned <- struct{}{}
		}
	}
	return calls
}

// heldDials holds every dial of a worker by the gateway.
type heldDials struct {
	// entered tells of each dial that waits.
	entered chan struct{}
	// cancelled tells of each dial the gateway gave up.
	cancelled chan struct{}
}

// holdDials makes every dial of a worker wait until the gateway gives it up, or the test ends.
func (f *fixtures) holdDials(t *testing.T) *heldDials {
	t.Helper()
	over := make(chan struct{})
	t.Cleanup(func() { close(over) })
	dials := &heldDials{entered: make(chan struct{}, 16), cancelled: make(chan struct{}, 16)}
	f.gateway.dial = func(ctx context.Context, address string) (net.Conn, error) {
		dials.entered <- struct{}{}
		select {
		case <-ctx.Done():
			dials.cancelled <- struct{}{}
			return nil, ctx.Err()
		case <-over:
			return nil, errors.New("the test is over")
		}
	}
	return dials
}

// dial connects to the node's tailnet listener, as a peer does.
func (n *fakeNode) dial(t *testing.T) net.Conn {
	t.Helper()
	peer, err := net.Dial("tcp", n.listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { peer.Close() })
	peer.SetReadDeadline(time.Now().Add(5 * time.Second))
	return peer
}

// line reads the next line the connection carries, or says how it ended instead.
func line(conn net.Conn) string {
	var got []byte
	for one := make([]byte, 1); ; {
		if _, err := conn.Read(one); err != nil {
			return fmt.Sprintf("%q then %v", got, err)
		}
		got = append(got, one[0])
		if one[0] == '\n' {
			return string(got)
		}
	}
}

// eventually waits for what the gateway does after a connection ended.
func eventually(t *testing.T, what string, done func() bool) {
	t.Helper()
	for deadline := time.Now().Add(5 * time.Second); !done(); time.Sleep(time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatalf("the gateway did not get to %s", what)
		}
	}
}

func TestPeerLoginMismatchClosesBeforeAnyConnectCall(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	node := f.serving(t, listed(idA))
	node.peerIs("eve@github", "laptop")

	peer := node.dial(t)

	if !closed(t, peer) {
		t.Error("the connection of a login the link does not admit is open")
	}
	if got := f.connects(); got != 0 {
		t.Errorf("the primary got %d connect calls for a login the link does not admit, want 0", got)
	}
}

func TestSecondAuthorizedLoginConnectsToSameNode(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	link := listed(idA)
	link.AllowedLogins = append(link.AllowedLogins, "alice@github")
	node := f.serving(t, link)
	for _, login := range link.AllowedLogins {
		node.peerIs(login, "laptop")
		if got := line(node.dial(t)); got != banner {
			t.Fatalf("%s read %q, want %q", login, got, banner)
		}
	}
	requests := f.primary.sent(connectRoute)
	if len(requests) != 2 || f.nodes.count() != 1 {
		t.Fatalf("got %d connects on %d nodes; want two connects on one node", len(requests), f.nodes.count())
	}
	for i, login := range link.AllowedLogins {
		var peer map[string]string
		if err := json.Unmarshal(requests[i].body, &peer); err != nil || peer["peer_login"] != login {
			t.Errorf("connect body = %s, want login %s", requests[i].body, login)
		}
	}
}

func TestOmittedLinkRefusesNewConnectionsAndDrainsOpenPipe(t *testing.T) {
	f := newFixtures(t)
	worker := f.wake(t)
	node := f.serving(t, listed(idA))
	peer := node.dial(t)
	upstream := worker.conn(t)
	if got := line(peer); got != banner {
		t.Fatalf("peer read %q, want %q", got, banner)
	}
	f.clock.advance(countedAfter)

	f.primary.list(t)
	f.reconcile(t)

	if !closed(t, node.dial(t)) || f.connects() != 1 {
		t.Fatal("an omitted link admitted a new connection")
	}
	if got := f.gateway.Status()[idA]; got != 1 {
		t.Fatalf("omitted link reports %d pipes, want 1", got)
	}
	fmt.Fprint(upstream, "still open\r\n")
	if got := line(peer); got != "still open\r\n" {
		t.Fatalf("existing pipe read %q after omission", got)
	}
	peer.Close()
	eventually(t, "drain the pipe", func() bool { return f.gateway.Status()[idA] == 0 })
	f.reconcile(t)
	if len(node.calls) != 1 || node.calls[0] != "Close" || !exists(t, node.dir) {
		t.Fatalf("drained node got %v; want close with state preserved", node.calls)
	}
}

func TestLinkRelistedDuringDrainUsesNewLoginsOnSameNode(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	link := listed(idA)
	node := f.serving(t, link)
	first := node.dial(t)
	if got := line(first); got != banner {
		t.Fatalf("first peer read %q, want %q", got, banner)
	}

	f.primary.list(t)
	f.reconcile(t)
	link.AllowedLogins = []string{"alice@github"}
	f.primary.list(t, link)
	f.reconcile(t)

	if f.nodes.count() != 1 || !closed(t, node.dial(t)) || f.connects() != 1 {
		t.Fatal("relisting restarted the draining node or admitted the old login")
	}
	node.peerIs("alice@github", "laptop")
	if got := line(node.dial(t)); got != banner || f.connects() != 2 {
		t.Fatalf("new login read %q after relisting; got %d connect calls", got, f.connects())
	}
}

func TestPeerTheTailnetDoesNotKnowIsRefused(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	f.primary.list(t, listed(idA))
	f.reconcile(t)

	peer := f.nodes.node(t, 0).dial(t)

	if !closed(t, peer) || f.connects() != 0 {
		t.Errorf("a peer WhoIs fails for is open or got %d connect calls, want it closed after 0", f.connects())
	}
}

func TestTaggedDeviceAndEmptyAllowedLoginsAreRefused(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	nobody := listed(idA)
	nobody.AllowedLogins = []string{}
	for name, test := range map[string]struct {
		link  Link
		login string
	}{
		"a tagged device":                          {listed(idA), ""},
		"a login on a link that admits nobody":     {nobody, "bas@github"},
		"a tagged device where nobody is admitted": {nobody, ""},
	} {
		node := f.serving(t, test.link)
		node.peerIs(test.login, "laptop")

		peer := node.dial(t)

		if !closed(t, peer) {
			t.Errorf("the connection of %s is open", name)
		}
	}
	if got := f.connects(); got != 0 {
		t.Errorf("the primary got %d connect calls, want 0", got)
	}
}

func TestPipeCarriesBytesBothWays(t *testing.T) {
	f := newFixtures(t)
	worker := f.wake(t)
	node := f.serving(t, listed(idA))

	peer := node.dial(t)
	greeting := line(peer)
	fmt.Fprint(peer, "SSH-2.0-peer\r\n")
	theirs := worker.conn(t)
	theirs.SetReadDeadline(time.Now().Add(5 * time.Second))

	if greeting != banner {
		t.Errorf("the peer read %q, want the worker's %q", greeting, banner)
	}
	if got := line(theirs); got != "SSH-2.0-peer\r\n" {
		t.Errorf("the worker read %q, want what the peer wrote", got)
	}
	want := jsonValue(t, fixture(t, "connect.json"))
	if got := jsonValue(t, f.primary.sent(connectRoute)[0].body); !reflect.DeepEqual(got, want) {
		t.Errorf("the connect call named the peer %v, want %v", got, want)
	}
}

func TestPreBannerLinesWhileConnectIsSlow(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	node := f.serving(t, listed(idA))
	connects := f.holdConnects(t)

	// The connect takes 7 s, in which 3 s pass twice.
	peer := node.dial(t)
	await(t, connects.entered, "the connect call")
	var lines []string
	for range 2 {
		f.clock.asleep(t, 1)
		f.clock.advance(3 * time.Second)
		lines = append(lines, line(peer))
	}
	f.clock.asleep(t, 1)
	f.clock.advance(time.Second)
	connects.answer()
	lines = append(lines, line(peer))

	if want := []string{waking, waking, banner}; !reflect.DeepEqual(lines, want) {
		t.Errorf("the peer read %q, want %q", lines, want)
	}
}

func TestPreBannerLinesUntilTheWorkerAnswers(t *testing.T) {
	f := newFixtures(t)
	worker := f.wake(t)
	greet := worker.holdGreetings(t)
	node := f.serving(t, listed(idA))

	// The primary answers at once, and the worker takes the connection and says nothing for 6 s.
	peer := node.dial(t)
	theirs := worker.conn(t)
	var lines []string
	for range 2 {
		f.clock.asleep(t, 1)
		f.clock.advance(3 * time.Second)
		lines = append(lines, line(peer))
	}
	greet()
	lines = append(lines, line(peer))
	// No line follows the worker's first bytes, however long the session lasts.
	f.clock.advance(3 * time.Second)
	fmt.Fprint(theirs, "later\r\n")
	lines = append(lines, line(peer))

	if want := []string{waking, waking, banner, "later\r\n"}; !reflect.DeepEqual(lines, want) {
		t.Errorf("the peer read %q, want %q", lines, want)
	}
}

// A worker connection that records the real budget and uses a short deadline for the test.
type deadlineConn struct {
	net.Conn
	deadlines chan time.Time
	short     time.Duration
}

func (c *deadlineConn) SetReadDeadline(at time.Time) error {
	c.deadlines <- at
	if !at.IsZero() {
		at = time.Now().Add(c.short)
	}
	return c.Conn.SetReadDeadline(at)
}

func (f *fixtures) workerDeadlines(short time.Duration) <-chan time.Time {
	deadlines := make(chan time.Time, 2)
	f.gateway.dial = func(ctx context.Context, addr string) (net.Conn, error) {
		conn, err := dialWorker(ctx, addr)
		if err != nil {
			return nil, err
		}
		return &deadlineConn{conn, deadlines, short}, nil
	}
	return deadlines
}

func TestWorkerThatSaysNothingHasA10SecondDeadline(t *testing.T) {
	f := newFixtures(t)
	worker := f.wake(t)
	worker.holdGreetings(t)
	deadlines := f.workerDeadlines(25 * time.Millisecond)
	node := f.serving(t, listed(idA))
	peer := node.dial(t)
	worker.conn(t)
	select {
	case deadline := <-deadlines:
		if got := time.Until(deadline).Round(time.Second); got != 10*time.Second {
			t.Errorf("worker read deadline is in %s, want 10 s", got)
		}
	case <-time.After(time.Second):
		t.Fatal("worker was not given a read deadline")
	}
	if !closed(t, peer) {
		t.Error("the gateway held a silent worker past its deadline")
	}
}

func TestWorkerDeadlineIsClearedBeforePiping(t *testing.T) {
	f := newFixtures(t)
	worker := f.wake(t)
	deadlines := f.workerDeadlines(time.Second)
	node := f.serving(t, listed(idA))
	peer := node.dial(t)
	if got := line(peer); got != banner {
		t.Fatalf("the peer read %q, want the worker's banner", got)
	}
	theirs := worker.conn(t)
	if first, cleared := <-deadlines, <-deadlines; first.IsZero() || !cleared.IsZero() {
		t.Fatalf("worker read deadlines = %v, %v; want set then cleared", first, cleared)
	}
	fmt.Fprint(theirs, "later\r\n")
	if got := line(peer); got != "later\r\n" {
		t.Fatalf("the peer read %q, want the bytes after the worker's banner", got)
	}
}

func TestPeerThatTakesNoPreBannerIsDropped(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	f.serving(t, listed(idA))
	connects := f.holdConnects(t)
	// A peer that reads nothing: a line written to it is not taken.
	ours, theirs := net.Pipe()
	t.Cleanup(func() {
		ours.Close()
		theirs.Close()
	})
	go f.gateway.carry(context.Background(), idA, f.gateway.nodes[idA], ours)
	await(t, connects.entered, "the connect call")

	// The first line is due, and the peer never consumes it.
	f.clock.asleep(t, 1)
	f.clock.advance(preBannerEvery)

	await(t, connects.abandoned, "give up the connect call of a peer that takes no line")
	if !closed(t, theirs) {
		t.Error("the connection of a peer that takes no line is open")
	}
}

func TestStalledPreBannerCancelsWorkerDial(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	dials := f.holdDials(t)
	f.serving(t, listed(idA))
	ours, theirs := net.Pipe()
	t.Cleanup(func() {
		ours.Close()
		theirs.Close()
	})
	go f.gateway.carry(context.Background(), idA, f.gateway.nodes[idA], ours)
	await(t, dials.entered, "dial the worker")
	f.clock.waitTimer(t, preBannerEvery)
	f.clock.advance(preBannerEvery)
	await(t, dials.cancelled, "cancel the worker dial when the pre-banner cannot be written")
	if !closed(t, theirs) {
		t.Error("the gateway left the stalled peer open")
	}
}

func TestFastConnectSendsNoPreBanner(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	node := f.serving(t, listed(idA))

	if got := line(node.dial(t)); got != banner {
		t.Errorf("the peer first read %q, want the worker's %q", got, banner)
	}
}

func TestSeventhConnectInAMinuteIsClosedWithoutCallingPrimary(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	node := f.serving(t, listed(idA))
	// Six connections, 10 s apart.
	for i := range 6 {
		if i > 0 {
			f.clock.advance(10 * time.Second)
		}
		if got := line(node.dial(t)); got != banner {
			t.Fatalf("connection %d read %q, want the worker's banner", i+1, got)
		}
	}

	// The first one is 59 s old.
	f.clock.advance(9 * time.Second)
	seventh := node.dial(t)
	if !closed(t, seventh) {
		t.Error("the seventh connection in a minute is open")
	}
	if got := f.connects(); got != 6 {
		t.Errorf("the primary got %d connect calls, want the first 6", got)
	}

	// And now 60 s, so it no longer counts, and the seventh never did.
	f.clock.advance(time.Second)
	if got := line(node.dial(t)); got != banner {
		t.Errorf("a connection a minute after the first read %q, want the worker's banner", got)
	}
}

func TestRefusalCloses(t *testing.T) {
	f := newFixtures(t)
	node := f.serving(t, listed(idA))
	f.primary.answer(connectRoute, http.StatusForbidden, `{"detail": "scope"}`)

	peer := node.dial(t)

	if !closed(t, peer) {
		t.Error("a connection the primary refused is open")
	}
}

func TestRetryAfter503CanConnect(t *testing.T) {
	f := newFixtures(t)
	node := f.serving(t, listed(idA))
	f.primary.answer(connectRoute, http.StatusServiceUnavailable, `{"detail": "Agent membership is not yet known; retry shortly."}`)
	if !closed(t, node.dial(t)) || f.connects() != 1 {
		t.Fatal("503 did not close the connection without an automatic retry")
	}
	f.wake(t)

	greeting := line(node.dial(t))

	if greeting != banner {
		t.Errorf("the peer read %q, want the worker's banner", greeting)
	}
}

func TestWorkerDialFailureCloses(t *testing.T) {
	f := newFixtures(t)
	worker := f.wake(t)
	worker.listener.Close()
	node := f.serving(t, listed(idA))

	peer := node.dial(t)

	if !closed(t, peer) {
		t.Error("a connection whose worker does not answer is open")
	}
}

func TestSlowWakeAndGraceCountNoOpenConnection(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	node := f.serving(t, listed(idA))
	connects := f.holdConnects(t)
	peer := node.dial(t)
	await(t, connects.entered, "the connect call")
	f.clock.asleep(t, 1)

	// The worker takes 90 s to wake: nobody has got to sshd, let alone logged in.
	f.clock.advance(90 * time.Second)
	woke := f.gateway.Status()[idA]
	first := line(peer)
	connects.answer()
	read := []string{first, line(peer)}
	// And from then sshd gives 60 s to log in.
	f.clock.advance(74 * time.Second)
	grace := f.gateway.Status()[idA]
	f.clock.advance(2 * time.Second)
	session := f.gateway.Status()[idA]

	if want := []string{waking, banner}; !reflect.DeepEqual(read, want) {
		t.Fatalf("the peer read %q, want %q", read, want)
	}
	if woke != 0 || grace != 0 || session != 1 {
		t.Errorf("open connections = %d after a 90 s wake, %d another 74 s on and %d at 76 s, want 0, 0 and 1",
			woke, grace, session)
	}
}

func TestOpenConnectionsCountsOnlyAfter75Seconds(t *testing.T) {
	f := newFixtures(t)
	f.wake(t)
	node := f.serving(t, listed(idA))
	peer := node.dial(t)
	if got := line(peer); got != banner {
		t.Fatalf("the peer read %q, want the worker's banner", got)
	}

	f.clock.advance(74 * time.Second)
	young := f.gateway.Status()[idA]
	f.clock.advance(2 * time.Second)
	old := f.gateway.Status()[idA]

	if young != 0 || old != 1 {
		t.Errorf("open connections = %d at 74 s and %d at 76 s, want 0 and 1", young, old)
	}
	peer.Close()
	eventually(t, "forget a connection its peer closed", func() bool {
		return f.gateway.Status()[idA] == 0
	})
}
