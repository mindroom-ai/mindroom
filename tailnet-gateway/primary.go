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

type RefusedError struct {
	StatusCode int    `json:"-"`
	Detail     string `json:"detail"`
}

func (e *RefusedError) Error() string {
	return fmt.Sprintf("primary answered %d: %s", e.StatusCode, e.Detail)
}

type Primary struct {
	baseURL, token string
	client         *http.Client
}

func NewPrimary(baseURL, token string) *Primary {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil // only the tailnet's control and relay traffic may use the proxy
	return &Primary{strings.TrimRight(baseURL, "/"), token, &http.Client{
		Transport:     transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (p *Primary) call(ctx context.Context, timeout time.Duration, method, path string, body, out any) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var sent io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		sent = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, p.baseURL+path, sent)
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
	answer, err := io.ReadAll(http.MaxBytesReader(nil, res.Body, maxAnswer))
	if err != nil {
		return err
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

func (p *Primary) Links(ctx context.Context) ([]DesiredLink, error) {
	var links []DesiredLink
	if err := p.call(ctx, callTimeout, http.MethodGet, "/links", nil, &links); err != nil {
		return nil, err
	}
	if links == nil { // JSON null is not permission to stop every node.
		return nil, errors.New("GET /links: the primary answered null, not a list")
	}
	return links, nil
}

func (p *Primary) PostStatus(ctx context.Context, report StatusReport) error {
	if report.OpenConnections == nil {
		report.OpenConnections = map[string]int{}
	}
	if report.Removed == nil {
		report.Removed = []string{}
	}
	return p.call(ctx, callTimeout, http.MethodPost, "/status", report, nil)
}

func (p *Primary) Connect(ctx context.Context, id, peerLogin, peerNode string) (string, int, error) {
	peer := map[string]string{"peer_login": peerLogin, "peer_node": peerNode}
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
