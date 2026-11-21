package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

const (
	connectTimeout = 610 * time.Second // worker wake can take 600 s
	callTimeout    = 10 * time.Second
	maxAnswer      = 4 << 20
)

type DesiredLink struct {
	ID             string  `json:"id"`
	Hostname       string  `json:"hostname"`
	Desired        string  `json:"desired"`
	AllowedLogin   *string `json:"allowed_login"` // nil enrolls the node but admits nobody
	AccessRevision string  `json:"access_revision"`
}

type StatusReport struct {
	OpenConnections map[string]int `json:"open_connections"`
	Removed         []string       `json:"removed"`
}

type Primary interface {
	Links(ctx context.Context) ([]DesiredLink, error)
	PostStatus(ctx context.Context, report StatusReport) error
	Connect(ctx context.Context, id, peerLogin, peerNode string) (host string, port int, err error)
}

type RefusedError struct {
	StatusCode int
	Detail     string `json:"detail"`
}

func (e *RefusedError) Error() string {
	return fmt.Sprintf("primary answered %d: %s", e.StatusCode, e.Detail)
}

type httpPrimary struct {
	baseURL string
	token   string
	client  *http.Client
}

func NewPrimary(baseURL, token string) Primary {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil // only the tailnet's control and relay traffic may use the proxy
	return &httpPrimary{strings.TrimRight(baseURL, "/"), token, &http.Client{
		Transport:     transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (p *httpPrimary) call(ctx context.Context, timeout time.Duration, method, path string, body, out any) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var raw []byte
	if body != nil {
		raw, _ = json.Marshal(body) // only the gateway's string and integer wire types
	}
	req, err := http.NewRequestWithContext(ctx, method, p.baseURL+path, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := p.client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if out == nil && res.StatusCode == http.StatusNoContent {
		return nil
	}
	answer, err := io.ReadAll(io.LimitReader(res.Body, maxAnswer+1))
	if err != nil {
		return err
	}
	if len(answer) > maxAnswer {
		return errors.New("primary response exceeds size limit")
	}
	if out != nil && res.StatusCode == http.StatusOK {
		return json.Unmarshal(answer, out)
	}
	refused := &RefusedError{StatusCode: res.StatusCode}
	if json.Unmarshal(answer, refused) != nil || refused.Detail == "" {
		refused.Detail = fmt.Sprintf("%d %s", res.StatusCode, http.StatusText(res.StatusCode))
	}
	return refused
}

func (p *httpPrimary) Links(ctx context.Context) ([]DesiredLink, error) {
	var links []DesiredLink
	if err := p.call(ctx, callTimeout, http.MethodGet, "/links", nil, &links); err != nil {
		return nil, err
	}
	if links == nil { // JSON null is not permission to stop every node.
		return nil, errors.New("GET /links: the primary answered null, not a list")
	}
	return links, nil
}

func (p *httpPrimary) PostStatus(ctx context.Context, report StatusReport) error {
	if report.OpenConnections == nil {
		report.OpenConnections = map[string]int{}
	}
	if report.Removed == nil {
		report.Removed = []string{}
	}
	return p.call(ctx, callTimeout, http.MethodPost, "/status", report, nil)
}

func (p *httpPrimary) Connect(ctx context.Context, id, peerLogin, peerNode string) (string, int, error) {
	peer := struct {
		PeerLogin string `json:"peer_login"`
		PeerNode  string `json:"peer_node"`
	}{peerLogin, peerNode}
	var target struct {
		Host string `json:"host"`
		Port int    `json:"port"`
	}
	if err := p.call(ctx, connectTimeout, http.MethodPost, "/links/"+id+"/connect", peer, &target); err != nil {
		return "", 0, err
	}
	if target.Host == "" || strings.TrimSpace(target.Host) != target.Host || target.Port < 1 || target.Port > 65535 {
		return "", 0, errors.New("the primary's 200 names no valid host and port")
	}
	return target.Host, target.Port, nil
}
