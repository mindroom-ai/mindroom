package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"net"
	"strconv"
	"time"
)

const (
	preBanner       = "MindRoom: waking worker\r\n" // RFC 4253, section 4.2
	preBannerEvery  = 3 * time.Second
	connectsAMinute = 6
	workerTimeout   = 10 * time.Second // dial and first bytes together
)

func (g *Gateway) serve(ctx context.Context, id string, node Node) {
	for {
		conn, err := node.Listener().Accept()
		if err != nil {
			if !errors.Is(err, net.ErrClosed) {
				log.Printf("link %s: accepting connections: %v", id, err)
			}
			return
		}
		go g.carry(ctx, id, node, conn)
	}
}

func (g *Gateway) carry(ctx context.Context, id string, node Node, conn net.Conn) {
	defer conn.Close()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	if !g.open(id, node, conn, cancel) {
		return
	}
	defer g.forget(id, conn)
	stopClosing := context.AfterFunc(ctx, func() { conn.Close() })
	defer stopClosing()

	whoCtx, whoDone := context.WithTimeout(ctx, nodeCallTimeout)
	login, peer, err := node.WhoIs(whoCtx, conn.RemoteAddr().String())
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
	go func() { woken <- g.wake(ctx, id, conn, login, peer) }()
	// Only this goroutine writes pre-banners; none can follow the worker's bytes.
	for tick := g.after(preBannerEvery); ; {
		select {
		case <-ctx.Done():
			return
		case to := <-woken:
			if to.sshd != nil && conn.SetWriteDeadline(time.Time{}) == nil {
				pipe(conn, to)
			}
			return
		case <-tick:
			if err := conn.SetWriteDeadline(time.Now().Add(preBannerEvery)); err != nil {
				return
			}
			if _, err := io.WriteString(conn, preBanner); err != nil {
				return
			}
			tick = g.after(preBannerEvery)
		}
	}
}

type worker struct {
	sshd  net.Conn
	first []byte
}

func pipe(conn net.Conn, to worker) {
	// Either copy finishing ends the pipe. carry then closes the peer and cancels the worker.
	done := make(chan struct{}, 2)
	copyTo := func(dst io.Writer, src io.Reader) {
		io.Copy(dst, src)
		done <- struct{}{}
	}
	go copyTo(conn, io.MultiReader(bytes.NewReader(to.first), to.sshd))
	go copyTo(to.sshd, conn)
	<-done
}

func (g *Gateway) wake(ctx context.Context, id string, conn net.Conn, login, peer string) worker {
	host, port, err := g.primary.Connect(ctx, id, login, peer)
	if err != nil {
		if ctx.Err() == nil {
			log.Printf("link %s: connect: %v", id, err)
		}
		return worker{}
	}
	if ctx.Err() != nil {
		return worker{}
	}
	inTime, cancel := context.WithTimeout(ctx, workerTimeout)
	defer cancel()
	sshd, err := g.dial(inTime, net.JoinHostPort(host, strconv.Itoa(port)))
	if err == nil {
		// The caller may already have left; close this dial on cancellation even then.
		context.AfterFunc(ctx, func() { sshd.Close() })
		g.mu.Lock()
		if kept := g.conns[id][conn]; kept != nil {
			kept.reached = g.now()
		}
		g.mu.Unlock()
		deadline, _ := inTime.Deadline()
		if err = sshd.SetReadDeadline(deadline); err == nil {
			first := make([]byte, 4096)
			var n int
			if n, err = sshd.Read(first); n > 0 {
				if err = sshd.SetReadDeadline(time.Time{}); err == nil {
					return worker{sshd, first[:n]}
				}
			}
		}
	}
	if ctx.Err() == nil {
		log.Printf("link %s: worker did not answer: %v", id, err)
	}
	return worker{}
}

func dialWorker(ctx context.Context, address string) (net.Conn, error) {
	var dialer net.Dialer
	return dialer.DialContext(ctx, "tcp", address)
}

func (g *Gateway) open(id string, node Node, conn net.Conn, cancel context.CancelFunc) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.links[id].Desired != "on" || g.nodes[id] != node {
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

func (g *Gateway) admit(id string, conn net.Conn, login string) string {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.conns[id][conn] == nil {
		return "the link's connections were revoked"
	}
	if allowed := g.links[id].AllowedLogin; login == "" || allowed == nil || *allowed != login {
		return "its peer is not the link's login"
	}
	now := g.now()
	recent := g.connects[id]
	for len(recent) > 0 && now.Sub(recent[0]) >= time.Minute {
		recent = recent[1:]
	}
	if len(recent) >= connectsAMinute {
		return "the link had its six connections of this minute"
	}
	g.connects[id] = append(recent, now)
	return ""
}
