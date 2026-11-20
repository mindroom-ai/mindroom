package main

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"strconv"
	"time"
)

const (
	// An SSH client skips lines before the server's version (RFC 4253, 4.2), and each one resets its connect
	// timeout, so a peer holds on while its worker starts.
	preBanner      = "MindRoom: waking worker\r\n"
	preBannerEvery = 3 * time.Second
	// A link gets this many connect calls in any minute.
	connectsAMinute = 6
	// How long a worker has to take a connection and send its first bytes; its sshd runs before the primary
	// names it.
	workerTimeout = 10 * time.Second
	// What the card shows of a link whose worker the primary named and the gateway could not reach.
	workerUnreachable = "The worker's SSH server could not be reached."
)

// serve accepts the connections of the link's node until its listener closes, each on its own.
//
// Only the link's id is taken from link: what the link admits is the row the primary listed last.
func (g *Gateway) serve(ctx context.Context, link DesiredLink, n Node) {
	listener := n.Listener()
	for {
		conn, err := listener.Accept()
		if err != nil {
			if !errors.Is(err, net.ErrClosed) {
				log.Printf("link %s: accepting connections: %v", link.ID, err)
			}
			return
		}
		go g.carry(ctx, link.ID, n, conn)
	}
}

// carry takes one accepted connection to the link's worker and closes it: the peer has to be the link's login, the
// primary has to name the worker, and until the worker's first bytes the peer is told to wait.
//
// It posts no status: it leaves the open connection and the link's last error for Run to report.
func (g *Gateway) carry(ctx context.Context, id string, n Node, conn net.Conn) {
	defer conn.Close()
	// Everything the connection waits for ends with it: when this returns, and when a pass closes the connection.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	if !g.open(id, conn, cancel) {
		log.Printf("link %s: closed a connection, the link takes none now", id)
		return
	}
	defer g.forget(id, conn)

	whoCtx, whoDone := context.WithTimeout(ctx, nodeCallTimeout)
	login, peer, err := n.WhoIs(whoCtx, conn.RemoteAddr().String())
	whoDone()
	if err != nil {
		log.Printf("link %s: closed a connection, its peer is unknown: %v", id, err)
		return
	}
	if refusal := g.admit(id, conn, login); refusal != "" {
		log.Printf("link %s: closed a connection, %s", id, refusal)
		return
	}

	woken := make(chan worker, 1)
	go func() { woken <- g.wake(ctx, cancel, id, conn, login, peer) }()
	// Until the worker's first bytes are in, this loop alone writes to the peer, one line at a time, and it is over
	// before they are sent on, so no line to wait follows them.
	var to worker
	for tick := g.clock.After(preBannerEvery); to.sshd == nil; {
		select {
		case to = <-woken:
			if to.sshd == nil {
				return
			}
			continue
		case <-tick:
		}
		// A peer has until the next line is due to take this one.
		tick = g.clock.After(preBannerEvery)
		wrote := make(chan error, 1)
		go func() {
			_, err := io.WriteString(conn, preBanner)
			wrote <- err
		}()
		select {
		case err := <-wrote:
			if err != nil {
				// The peer left, or a pass closed the connection.
				return
			}
		case <-tick:
			log.Printf("link %s: closed a connection, its peer does not take what it is sent", id)
			return
		}
	}

	// Either side closing ends both copies: this one returns, which closes both ends.
	done := make(chan struct{}, 2)
	go func() {
		if _, err := conn.Write(to.first); err == nil {
			io.Copy(conn, to.sshd)
		}
		done <- struct{}{}
	}()
	go func() {
		io.Copy(to.sshd, conn)
		done <- struct{}{}
	}()
	<-done
}

// worker is a worker's end of a connection, and the first bytes the worker sent on it; without sshd the connection
// did not get to its worker.
type worker struct {
	sshd  net.Conn
	first []byte
}

// wake asks the primary where the connection goes and reaches the worker it names, which may take the primary as
// long as a worker takes to start, and the worker workerTimeout to take the connection and send its first bytes.
//
// ctx is the connection's: the worker's end is closed when it ends, which giveUp makes it do.
func (g *Gateway) wake(ctx context.Context, giveUp func(), id string, conn net.Conn, login, peer string) worker {
	host, port, err := g.primary.Connect(ctx, id, login, peer)
	var refused *RefusedError
	if errors.As(err, &refused) {
		if g.settle(id, conn, refused.Detail) {
			log.Printf("link %s: closed a connection, the primary refused it: %s", id, refused.Detail)
		}
		return worker{}
	}
	if err != nil {
		if ctx.Err() == nil {
			log.Printf("link %s: closed a connection, the primary did not answer: %v", id, err)
		}
		return worker{}
	}
	if !g.settle(id, conn, "") {
		// A pass closed the connection while its worker woke, so it is not taken there.
		return worker{}
	}

	answered := make(chan struct{})
	defer close(answered)
	go func() {
		select {
		case <-g.clock.After(workerTimeout):
			giveUp()
		case <-answered:
		}
	}()
	sshd, err := g.dial(ctx, net.JoinHostPort(host, strconv.Itoa(port)))
	if err == nil {
		context.AfterFunc(ctx, func() { sshd.Close() })
		if !g.reach(id, conn) {
			return worker{}
		}
		first := make([]byte, 4096)
		var n int
		if n, err = sshd.Read(first); err == nil {
			return worker{sshd, first[:n]}
		}
	}
	if g.settle(id, conn, workerUnreachable) {
		log.Printf("link %s: closed a connection, its worker did not answer: %v", id, err)
	}
	return worker{}
}

// dialWorker reaches a worker from the gateway's own network: no tailnet address is dialled through a node.
func dialWorker(ctx context.Context, address string) (net.Conn, error) {
	var dialer net.Dialer
	return dialer.DialContext(ctx, "tcp", address)
}

// open keeps the connection as one of the link's, which a pass that revokes the link's connections closes, calling
// cancel, and says whether it did: only a link listed as on takes a connection.
func (g *Gateway) open(id string, conn net.Conn, cancel context.CancelFunc) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.links[id].Desired != "on" {
		return false
	}
	if g.conns[id] == nil {
		g.conns[id] = map[net.Conn]*held{}
	}
	g.conns[id][conn] = &held{cancel: cancel}
	return true
}

func (g *Gateway) forget(id string, conn net.Conn) {
	g.mu.Lock()
	defer g.mu.Unlock()
	delete(g.conns[id], conn)
}

// reach notes that the connection got to its worker now, from when Status counts its age, and says whether it is
// still one of the link's.
func (g *Gateway) reach(id string, conn net.Conn) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	kept := g.conns[id][conn]
	if kept == nil {
		return false
	}
	kept.reached = g.clock.Now()
	return true
}

// admit says why the connection of the peer with this login gets no connect call, or "" when it gets one, which
// then counts as one of the link's six a minute.
func (g *Gateway) admit(id string, conn net.Conn, login string) (refusal string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, open := g.conns[id][conn]; !open {
		return "a pass closed the link's connections"
	}
	// A tagged device has no login, and a link without one admits nobody.
	if allowed := g.links[id].AllowedLogin; login == "" || allowed == nil || *allowed != login {
		return "its peer is not the link's login"
	}
	now := g.clock.Now()
	var recent []time.Time
	for _, at := range g.connects[id] {
		if now.Sub(at) < time.Minute {
			recent = append(recent, at)
		}
	}
	if len(recent) >= connectsAMinute {
		g.connects[id] = recent
		return "the link had its six connections of this minute"
	}
	g.connects[id] = append(recent, now)
	return ""
}

// settle keeps detail as the link's last error, or no error for "", and says whether the connection is still one
// of the link's; a connection a pass closed changes nothing.
func (g *Gateway) settle(id string, conn net.Conn, detail string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, open := g.conns[id][conn]; !open {
		return false
	}
	if detail == "" {
		delete(g.lastError, id)
	} else {
		g.lastError[id] = detail
	}
	return true
}
