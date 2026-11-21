package main

import (
	"bufio"
	"context"
	"io"
	"net"
	"slices"
	"strconv"
	"time"
)

const (
	preBanner       = "MindRoom: waking worker\r\n" // RFC 4253, section 4.2
	preBannerEvery  = 3 * time.Second
	connectsAMinute = 6
	workerTimeout   = 10 * time.Second // dial and first bytes together
)

func (g *Gateway) serve(ctx context.Context, id string, node *linkNode) {
	for {
		conn, err := node.Listener().Accept()
		if err != nil {
			return
		}
		go g.carry(ctx, id, node, conn)
	}
}

func (g *Gateway) admit(node *linkNode, conn net.Conn, login string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := g.now()
	if login == "" || !slices.Contains(node.logins, login) || now.Before(node.recent[node.next].Add(time.Minute)) {
		return false
	}
	node.recent[node.next] = now
	node.next = (node.next + 1) % connectsAMinute
	node.open[conn] = time.Time{}
	return true
}

func (g *Gateway) carry(ctx context.Context, id string, node *linkNode, conn net.Conn) {
	defer conn.Close()
	whoCtx, whoDone := context.WithTimeout(ctx, callTimeout)
	login, peer, err := node.WhoIs(whoCtx, conn.RemoteAddr().String())
	whoDone()
	if err != nil || !g.admit(node, conn, login) {
		return
	}
	defer func() {
		g.mu.Lock()
		delete(node.open, conn)
		g.mu.Unlock()
	}()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer context.AfterFunc(ctx, func() { conn.Close() })()
	ready := make(chan worker, 1)
	go func() { ready <- g.wake(ctx, id, login, peer) }()
	for {
		select {
		case <-ctx.Done():
			return
		case to := <-ready:
			if to.Conn == nil || conn.SetWriteDeadline(time.Time{}) != nil {
				return
			}
			g.mu.Lock()
			node.open[conn] = to.reached
			g.mu.Unlock()
			go func() {
				defer cancel()
				io.Copy(to, conn)
			}()
			io.Copy(conn, to.reader)
			return
		case <-g.after(preBannerEvery):
			if conn.SetWriteDeadline(time.Now().Add(preBannerEvery)) != nil {
				return
			}
			if _, err := io.WriteString(conn, preBanner); err != nil {
				return
			}
		}
	}
}

type worker struct {
	net.Conn
	reader  *bufio.Reader
	reached time.Time
}

func (g *Gateway) wake(ctx context.Context, id, login, peer string) worker {
	host, port, err := g.primary.Connect(ctx, id, login, peer)
	if err != nil {
		return worker{}
	}
	inTime, cancel := context.WithTimeout(ctx, workerTimeout)
	defer cancel()
	sshd, err := g.dial(inTime, net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		return worker{}
	}
	context.AfterFunc(ctx, func() { sshd.Close() })
	to := worker{sshd, bufio.NewReader(sshd), g.now()}
	deadline, _ := inTime.Deadline()
	if sshd.SetReadDeadline(deadline) != nil {
		return worker{}
	}
	if _, err := to.reader.Peek(1); err != nil || sshd.SetReadDeadline(time.Time{}) != nil {
		return worker{}
	}
	return to
}

func dialWorker(ctx context.Context, address string) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, "tcp", address)
}
