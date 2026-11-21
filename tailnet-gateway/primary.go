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

type Link struct {
	ID            string   `json:"id"`
	Hostname      string   `json:"hostname"`
	AllowedLogins []string `json:"allowed_logins"`
}

type StatusReport struct {
	OpenConnections map[string]int `json:"open_connections"`
}

type RefusedError int

func (e RefusedError) Error() string { return fmt.Sprintf("primary answered HTTP %d", int(e)) }

type Primary struct {
	baseURL, token string
	client         *http.Client
}

func NewPrimary(baseURL, token string) *Primary {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil // never send the gateway token through a proxy
	return &Primary{strings.TrimRight(baseURL, "/"), token, &http.Client{
		Transport:     transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (p *Primary) call(ctx context.Context, timeout time.Duration, method, path string, body []byte, out any) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, p.baseURL+path, bytes.NewReader(body))
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
	if out == nil && (res.StatusCode == http.StatusNoContent || res.StatusCode == http.StatusOK) {
		return nil
	}
	if res.StatusCode != http.StatusOK {
		return RefusedError(res.StatusCode)
	}
	answer, err := io.ReadAll(http.MaxBytesReader(nil, res.Body, maxAnswer))
	if err != nil {
		return err
	}
	return json.Unmarshal(answer, out)
}

func (p *Primary) Links(ctx context.Context) ([]Link, error) {
	var links []Link
	if err := p.call(ctx, callTimeout, http.MethodGet, "/links", nil, &links); err != nil {
		return nil, err
	}
	if links == nil {
		return nil, errors.New("GET /links: expected a list, not null")
	}
	return links, nil
}

func (p *Primary) PostStatus(ctx context.Context, report StatusReport) error {
	if report.OpenConnections == nil {
		report.OpenConnections = map[string]int{}
	}
	body, _ := json.Marshal(report) // only string keys and integer counts
	return p.call(ctx, callTimeout, http.MethodPost, "/status", body, nil)
}

func (p *Primary) Connect(ctx context.Context, id, login, peer string) (string, int, error) {
	body, _ := json.Marshal(map[string]string{"peer_login": login, "peer_node": peer})
	var to struct {
		Host string `json:"host"`
		Port int    `json:"port"`
	}
	if err := p.call(ctx, connectTimeout, http.MethodPost, "/links/"+id+"/connect", body, &to); err != nil {
		return "", 0, err
	}
	if to.Host == "" || strings.TrimSpace(to.Host) != to.Host || to.Port < 1 || to.Port > 65535 {
		return "", 0, errors.New("the primary's 200 names no valid host and port")
	}
	return to.Host, to.Port, nil
}
