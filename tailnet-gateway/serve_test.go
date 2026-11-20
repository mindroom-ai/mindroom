package main

import (
	"context"
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
func (f *fixtures) serving(t *testing.T, link DesiredLink) *fakeNode {
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
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))
	node.peerIs("eve@github", "laptop")

	peer := node.dial(t)

	if !closed(t, peer) {
		t.Error("the connection of a login the link does not admit is open")
	}
	if got := f.connects(); got != 0 {
		t.Errorf("the primary got %d connect calls for a login the link does not admit, want 0", got)
	}
}

func TestPeerTheTailnetDoesNotKnowIsRefused(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	f.primary.list(t, on(idA))
	f.reconcile(t)

	peer := f.nodes.node(t, 0).dial(t)

	if !closed(t, peer) || f.connects() != 0 {
		t.Errorf("a peer WhoIs fails for is open or got %d connect calls, want it closed after 0", f.connects())
	}
}

func TestTaggedDeviceAndNilAllowedLoginAreRefused(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	nobody := on(idA)
	nobody.AllowedLogin = nil
	for name, test := range map[string]struct {
		link  DesiredLink
		login string
	}{
		"a tagged device":                          {on(idA), ""},
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
	f := newFixtures(t, "login")
	worker := f.wake(t)
	node := f.serving(t, on(idA))

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
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))
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
	f := newFixtures(t, "login")
	worker := f.wake(t)
	greet := worker.holdGreetings(t)
	node := f.serving(t, on(idA))

	// The primary answers at once, and the worker takes the connection and says nothing for 6 s.
	peer := node.dial(t)
	theirs := worker.conn(t)
	var lines []string
	for range 2 {
		f.clock.asleep(t, 2)
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

func TestWorkerThatSaysNothingIsGivenUpAfter10Seconds(t *testing.T) {
	f := newFixtures(t, "login")
	worker := f.wake(t)
	worker.holdGreetings(t)
	node := f.serving(t, on(idA))
	peer := node.dial(t)
	worker.conn(t)
	f.clock.asleep(t, 2)

	f.clock.advance(9 * time.Second)
	early := line(peer)
	f.clock.asleep(t, 2)
	f.clock.advance(time.Second)

	eventually(t, "give up a worker that says nothing", func() bool {
		got := f.gateway.Status()[idA].LastError
		return got != nil && *got == workerUnreachable
	})
	if early != waking {
		t.Errorf("the peer read %q after 9 s, want it told to wait", early)
	}
	if !closed(t, peer) {
		t.Error("the connection of a worker that says nothing is open")
	}
}

func TestWorkerThatAnswersAsItsTimeIsUpKeepsTheConnectionItGot(t *testing.T) {
	f := newFixtures(t, "login")
	worker := f.wake(t)
	node := f.serving(t, on(idA))
	// Which of the two comes first is not the test's to say, so it is tried often: whenever the peer got the
	// worker's first bytes, the connection is the worker's and its time for them ends nothing any more.
	for try := range 300 {
		greet := worker.holdGreetings(t)
		peer := node.dial(t)
		theirs := worker.conn(t)

		go greet()
		f.clock.advance(10 * time.Second)

		got := line(peer)
		for got == waking {
			got = line(peer)
		}
		if got == banner {
			fmt.Fprint(theirs, "later\r\n")
			if after := line(peer); after != "later\r\n" {
				t.Fatalf("try %d: the peer got the worker's banner and then read %q, want what the worker sent next", try, after)
			}
		}
		peer.Close()
		// Past the minute that limits the link's connect calls.
		f.clock.advance(time.Minute)
	}
}

func TestPeerThatTakesNoPreBannerIsDropped(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))
	connects := f.holdConnects(t)
	// A peer that reads nothing: a line written to it is not taken.
	ours, theirs := net.Pipe()
	t.Cleanup(func() {
		ours.Close()
		theirs.Close()
	})
	go f.gateway.carry(context.Background(), idA, node, ours)
	await(t, connects.entered, "the connect call")

	// The first line is due, and is not taken by the time the second is.
	for range 2 {
		f.clock.asleep(t, 1)
		f.clock.advance(3 * time.Second)
	}

	await(t, connects.abandoned, "give up the connect call of a peer that takes no line")
	if !closed(t, theirs) {
		t.Error("the connection of a peer that takes no line is open")
	}
}

func TestFastConnectSendsNoPreBanner(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))

	if got := line(node.dial(t)); got != banner {
		t.Errorf("the peer first read %q, want the worker's %q", got, banner)
	}
}

func TestSeventhConnectInAMinuteIsClosedWithoutCallingPrimary(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))
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

func TestRefusalClosesAndSetsLastError(t *testing.T) {
	f := newFixtures(t, "login")
	node := f.serving(t, on(idA))
	f.primary.answer(connectRoute, http.StatusForbidden, `{"detail": "scope"}`)

	peer := node.dial(t)

	if !closed(t, peer) {
		t.Error("a connection the primary refused is open")
	}
	if got := f.gateway.Status()[idA].LastError; got == nil || *got != "scope" {
		t.Errorf("last error = %v, want scope", got)
	}
}

func TestConnectionThatGetsThroughClearsLastError(t *testing.T) {
	f := newFixtures(t, "login")
	node := f.serving(t, on(idA))
	f.primary.answer(connectRoute, http.StatusForbidden, `{"detail": "scope"}`)
	closed(t, node.dial(t))
	f.wake(t)

	greeting := line(node.dial(t))

	if greeting != banner {
		t.Errorf("the peer read %q, want the worker's banner", greeting)
	}
	if got := f.gateway.Status()[idA].LastError; got != nil {
		t.Errorf("last error = %q after a connection got through, want none", *got)
	}
}

func TestWorkerThatDoesNotAnswerClosesAndSetsLastError(t *testing.T) {
	f := newFixtures(t, "login")
	worker := f.wake(t)
	worker.listener.Close()
	node := f.serving(t, on(idA))

	peer := node.dial(t)

	if !closed(t, peer) {
		t.Error("a connection whose worker does not answer is open")
	}
	if got := f.gateway.Status()[idA].LastError; got == nil || *got != workerUnreachable {
		t.Errorf("last error = %v, want %q", got, workerUnreachable)
	}
}

func TestClosedBeforeChangeClosesAllOpenConnections(t *testing.T) {
	f := newFixtures(t, "login")
	worker := f.wake(t)
	link := on(idA)
	link.ClosedBefore = float64(f.clock.Now().Unix() + 500)
	node := f.serving(t, link)
	peer := node.dial(t)
	if got := line(peer); got != banner {
		t.Fatalf("the peer read %q, want the worker's banner", got)
	}
	theirs := worker.conn(t)
	theirs.SetReadDeadline(time.Now().Add(5 * time.Second))

	// A pass with the value the connection was opened under closes nothing.
	f.reconcile(t)
	fmt.Fprint(peer, "still here\r\n")
	if got := line(theirs); got != "still here\r\n" {
		t.Fatalf("the worker read %q after a pass that changed nothing, want what the peer wrote", got)
	}

	// The value moves to a time before this clock saw the connection opened: it is closed all the same.
	link.ClosedBefore = float64(f.clock.Now().Unix() - 500)
	f.primary.list(t, link)
	f.reconcile(t)

	if !closed(t, peer) {
		t.Error("the connection is open after the link's closed_before changed")
	}
}

func TestSuspendedRefusesNewAndClosesOpen(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))
	open := node.dial(t)
	if got := line(open); got != banner {
		t.Fatalf("the peer read %q, want the worker's banner", got)
	}
	suspended := on(idA)
	suspended.Desired = "suspended"
	f.primary.list(t, suspended)

	f.reconcile(t)
	late := node.dial(t)

	if !closed(t, open) {
		t.Error("the open connection of a suspended link is open")
	}
	if !closed(t, late) {
		t.Error("a suspended link took a new connection")
	}
	if got := f.connects(); got != 1 {
		t.Errorf("the primary got %d connect calls, want the 1 from before the link was suspended", got)
	}
}

func TestConnectionRevokedWhileItsWorkerWakesNeverReachesTheWorker(t *testing.T) {
	f := newFixtures(t, "login")
	worker := f.wake(t)
	node := f.serving(t, on(idA))
	connects := f.holdConnects(t)
	peer := node.dial(t)
	await(t, connects.entered, "the connect call")
	suspended := on(idA)
	suspended.Desired = "suspended"
	f.primary.list(t, suspended)

	f.reconcile(t)

	await(t, connects.abandoned, "give up the connect call of a connection a pass closed")
	connects.answer()
	if !closed(t, peer) {
		t.Error("the connection of a link suspended while its worker woke is open")
	}
	select {
	case <-worker.accepted:
		t.Error("the gateway reached the worker for a connection of a suspended link")
	case <-time.After(100 * time.Millisecond):
	}
}

func TestConnectionRevokedWhileItsWorkerIsDialledGivesTheDialUp(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	dials := f.holdDials(t)
	node := f.serving(t, on(idA))
	peer := node.dial(t)
	// The primary has named the worker, and the gateway is reaching it.
	await(t, dials.entered, "the dial of the worker")
	suspended := on(idA)
	suspended.Desired = "suspended"
	f.primary.list(t, suspended)

	f.reconcile(t)

	await(t, dials.cancelled, "give up the dial of a connection a pass closed")
	if !closed(t, peer) {
		t.Error("the connection of a link suspended while its worker was dialled is open")
	}
	if got := f.gateway.Status()[idA].LastError; got != nil {
		t.Errorf("last error = %q, want none for a connection a pass closed", *got)
	}
}

func TestRemovingLinkTakesNoNewConnection(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	// The removal waits for the node's logout, so its listener is still open.
	loggingOut, _ := f.nodes.holdLogouts(t)
	f.primary.list(t, on(idA))
	f.run(t)
	node := f.nodes.node(t, 0)
	node.peerIs("bas@github", "laptop")
	f.primary.list(t, removing(idA))
	f.clock.advance(5 * time.Second)
	await(t, loggingOut, "the logout of the removed link's node")

	peer := node.dial(t)

	if !closed(t, peer) {
		t.Error("a link that is being removed took a new connection")
	}
	f.gateway.mu.Lock()
	open := len(f.gateway.conns[idA])
	f.gateway.mu.Unlock()
	if open != 0 || f.connects() != 0 {
		t.Errorf("the link being removed has %d connections after %d connect calls, want none", open, f.connects())
	}
}

func TestSlowWakeAndGraceCountNoOpenConnection(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))
	connects := f.holdConnects(t)
	peer := node.dial(t)
	await(t, connects.entered, "the connect call")
	f.clock.asleep(t, 1)

	// The worker takes 90 s to wake: nobody has got to sshd, let alone logged in.
	f.clock.advance(90 * time.Second)
	woke := f.gateway.Status()[idA].OpenConnections
	connects.answer()
	read := []string{line(peer), line(peer)}
	// And from then sshd gives 60 s to log in.
	f.clock.advance(74 * time.Second)
	grace := f.gateway.Status()[idA].OpenConnections
	f.clock.advance(2 * time.Second)
	session := f.gateway.Status()[idA].OpenConnections

	if want := []string{waking, banner}; !reflect.DeepEqual(read, want) {
		t.Fatalf("the peer read %q, want %q", read, want)
	}
	if woke != 0 || grace != 0 || session != 1 {
		t.Errorf("open connections = %d after a 90 s wake, %d another 74 s on and %d at 76 s, want 0, 0 and 1",
			woke, grace, session)
	}
}

func TestOpenConnectionsCountsOnlyAfter75Seconds(t *testing.T) {
	f := newFixtures(t, "login")
	f.wake(t)
	node := f.serving(t, on(idA))
	peer := node.dial(t)
	if got := line(peer); got != banner {
		t.Fatalf("the peer read %q, want the worker's banner", got)
	}

	f.clock.advance(74 * time.Second)
	young := f.gateway.Status()[idA].OpenConnections
	f.clock.advance(2 * time.Second)
	old := f.gateway.Status()[idA].OpenConnections

	if young != 0 || old != 1 {
		t.Errorf("open connections = %d at 74 s and %d at 76 s, want 0 and 1", young, old)
	}
	peer.Close()
	eventually(t, "forget a connection its peer closed", func() bool {
		return f.gateway.Status()[idA].OpenConnections == 0
	})
}
