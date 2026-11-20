package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	// A connect may wait as long as the primary's worker ready timeout (600 s hosted) for a worker to start.
	connectTimeout = 610 * time.Second
	callTimeout    = 10 * time.Second
)

// DesiredLink is one row of the primary's GET /links: a link and what the gateway is to do with its node.
type DesiredLink struct {
	ID       string `json:"id"`
	Hostname string `json:"hostname"`
	// Desired is "on", "suspended" or "removing".
	Desired string `json:"desired"`
	// AllowedLogin is the one tailnet login the link admits; nil admits nobody.
	AllowedLogin *string `json:"allowed_login"`
	ClosedBefore float64 `json:"closed_before"`
	LoginAfter   float64 `json:"login_after"`
}

// LinkStatus is what the gateway reports of one link's node in POST /status.
type LinkStatus struct {
	// State is "needs_login", "needs_approval", "running" or "removed".
	State           string  `json:"state"`
	LoginURL        *string `json:"login_url"`
	Tailnet         *string `json:"tailnet"`
	DNSName         *string `json:"dns_name"`
	LoginName       *string `json:"login_name"`
	KeyExpiry       *string `json:"key_expiry"`
	LastError       *string `json:"last_error"`
	OpenConnections int     `json:"open_connections"`
}

// Primary is the primary's internal listener, the only thing the gateway asks what to do.
type Primary interface {
	// Links returns the links the gateway is to hold.
	Links(ctx context.Context) ([]DesiredLink, error)
	// PostStatus reports how the gateway enrols its nodes ("login" or "auth_key") and every node it holds.
	PostStatus(ctx context.Context, enrollment string, links map[string]LinkStatus) error
	// Connect asks where an accepted connection of the link goes; a *RefusedError says the primary refused it.
	Connect(ctx context.Context, id, peerLogin, peerNode string) (host string, port int, err error)
}

// RefusedError is the primary's refusal of a connect; Detail is what it answered, kept as the link's last error.
type RefusedError struct{ Detail string }

func (e *RefusedError) Error() string { return "the primary refused the connection: " + e.Detail }

type httpPrimary struct {
	baseURL string
	token   string
	client  *http.Client
}

// NewPrimary returns the client of the primary's internal listener at baseURL, which takes the bearer token.
func NewPrimary(baseURL, token string) Primary {
	// The environment's proxy is for the tailnet's control and relay servers; the token goes to the primary alone.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &httpPrimary{
		baseURL: strings.TrimRight(baseURL, "/"),
		token:   token,
		client:  &http.Client{Transport: transport},
	}
}

// call sends one request, with body as JSON unless it is nil, and returns the answer's status code and body.
func (p *httpPrimary) call(ctx context.Context, timeout time.Duration, method, path string, body any) (int, []byte, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var sent io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		sent = bytes.NewReader(raw)
	}
	request, err := http.NewRequestWithContext(ctx, method, p.baseURL+path, sent)
	if err != nil {
		return 0, nil, err
	}
	request.Header.Set("Authorization", "Bearer "+p.token)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := p.client.Do(request)
	if err != nil {
		return 0, nil, err
	}
	defer response.Body.Close()
	answer, err := io.ReadAll(response.Body)
	return response.StatusCode, answer, err
}

func (p *httpPrimary) Links(ctx context.Context) ([]DesiredLink, error) {
	code, answer, err := p.call(ctx, callTimeout, http.MethodGet, "/links", nil)
	if err != nil {
		return nil, err
	}
	if code != http.StatusOK {
		return nil, fmt.Errorf("GET /links: the primary answered %d", code)
	}
	var links []DesiredLink
	if err := json.Unmarshal(answer, &links); err != nil {
		return nil, fmt.Errorf("GET /links: %w", err)
	}
	if links == nil {
		// JSON null decodes without an error; taken as an empty list it would stop every node.
		return nil, errors.New("GET /links: the primary answered null, not a list")
	}
	return links, nil
}

func (p *httpPrimary) PostStatus(ctx context.Context, enrollment string, links map[string]LinkStatus) error {
	if links == nil {
		// The primary takes an object, and nil would be sent as null.
		links = map[string]LinkStatus{}
	}
	report := struct {
		Enrollment string                `json:"enrollment"`
		Links      map[string]LinkStatus `json:"links"`
	}{enrollment, links}
	code, _, err := p.call(ctx, callTimeout, http.MethodPost, "/status", report)
	if err != nil {
		return err
	}
	if code != http.StatusNoContent {
		return fmt.Errorf("POST /status: the primary answered %d", code)
	}
	return nil
}

func (p *httpPrimary) Connect(ctx context.Context, id, peerLogin, peerNode string) (string, int, error) {
	peer := struct {
		PeerLogin string `json:"peer_login"`
		PeerNode  string `json:"peer_node"`
	}{peerLogin, peerNode}
	code, answer, err := p.call(ctx, connectTimeout, http.MethodPost, "/links/"+url.PathEscape(id)+"/connect", peer)
	if err != nil {
		return "", 0, err
	}
	if code != http.StatusOK {
		var refusal struct {
			Detail string `json:"detail"`
		}
		if json.Unmarshal(answer, &refusal) != nil || refusal.Detail == "" {
			refusal.Detail = fmt.Sprintf("%d %s", code, http.StatusText(code))
		}
		return "", 0, &RefusedError{Detail: refusal.Detail}
	}
	var target struct {
		Host string `json:"host"`
		Port int    `json:"port"`
	}
	if err := json.Unmarshal(answer, &target); err != nil {
		return "", 0, fmt.Errorf("POST /links/%s/connect: %w", id, err)
	}
	if target.Host == "" || target.Port < 1 || target.Port > 65535 {
		return "", 0, fmt.Errorf("POST /links/%s/connect: the primary's 200 names no host and port", id)
	}
	return target.Host, target.Port, nil
}
