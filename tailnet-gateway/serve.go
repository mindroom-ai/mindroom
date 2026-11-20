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
	dialTimeout     = 10 * time.Second
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
// primary has to name the worker, and until then the peer is told to wait.
//
// It posts no status: it leaves the open connection and the link's last error for Run to report.
func (g *Gateway) carry(ctx context.Context, id string, n Node, conn net.Conn) {
	defer conn.Close()
	if !g.open(id, conn) {
		log.Printf("link %s: closed a connection, the link takes none now", id)
		return
	}
	defer g.forget(id, conn)

	whoCtx, cancel := context.WithTimeout(ctx, nodeCallTimeout)
	login, peer, err := n.WhoIs(whoCtx, conn.RemoteAddr().String())
	cancel()
	if err != nil {
		log.Printf("link %s: closed a connection, its peer is unknown: %v", id, err)
		return
	}
	if refusal := g.admit(id, conn, login); refusal != "" {
		log.Printf("link %s: closed a connection, %s", id, refusal)
		return
	}

	type target struct {
		host string
		port int
		err  error
	}
	connectCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	answered := make(chan target, 1)
	go func() {
		host, port, err := g.primary.Connect(connectCtx, id, login, peer)
		answered <- target{host, port, err}
	}()
	var worker target
	for waiting := true; waiting; {
		select {
		case worker = <-answered:
			waiting = false
		case <-g.clock.After(preBannerEvery):
			if _, err := io.WriteString(conn, preBanner); err != nil {
				// The peer left, or a pass closed the connection.
				return
			}
		}
	}
	var refused *RefusedError
	if errors.As(worker.err, &refused) {
		g.settle(id, conn, refused.Detail)
		log.Printf("link %s: closed a connection, the primary refused it: %s", id, refused.Detail)
		return
	}
	if worker.err != nil {
		log.Printf("link %s: closed a connection, the primary did not answer: %v", id, worker.err)
		return
	}
	if !g.settle(id, conn, "") {
		// A pass closed the connection while its worker woke, so it is not taken there.
		return
	}
	dialer := net.Dialer{Timeout: dialTimeout}
	sshd, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(worker.host, strconv.Itoa(worker.port)))
	if err != nil {
		g.settle(id, conn, workerUnreachable)
		log.Printf("link %s: closed a connection, its worker did not answer: %v", id, err)
		return
	}
	defer sshd.Close()

	// Either side closing ends both copies: this one returns, which closes both ends.
	done := make(chan struct{}, 2)
	relay := func(to, from net.Conn) {
		io.Copy(to, from)
		done <- struct{}{}
	}
	go relay(sshd, conn)
	go relay(conn, sshd)
	<-done
}

// open keeps the connection as one of the link's, which Status counts and a pass that revokes the link's
// connections closes, and says whether it did: only a link listed as on takes a connection.
func (g *Gateway) open(id string, conn net.Conn) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.links[id].Desired != "on" {
		return false
	}
	if g.conns[id] == nil {
		g.conns[id] = map[net.Conn]time.Time{}
	}
	g.conns[id][conn] = g.clock.Now()
	return true
}

func (g *Gateway) forget(id string, conn net.Conn) {
	g.mu.Lock()
	defer g.mu.Unlock()
	delete(g.conns[id], conn)
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
