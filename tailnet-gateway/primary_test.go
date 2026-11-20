package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	idA = "0123456789abcdef"
	idB = "fedcba9876543210"
	idC = "0011223344556677"
)

// fakePrimary is the primary's internal listener as the gateway sees it: an empty list of links and a 204 for a
// status report, until a test sets another answer.
type fakePrimary struct {
	*httptest.Server
	mu       sync.Mutex
	answers  map[string]fakeAnswer
	requests []fakeRequest
	// onStatus, when set, runs while a POST /status waits for its answer.
	onStatus func()
	// onConnect, when set, runs while a POST /links/{id}/connect waits for its answer.
	onConnect func()
}

type fakeAnswer struct {
	code int
	body string
}

type fakeRequest struct {
	route         string
	authorization string
	body          []byte
}

func newFakePrimary(t *testing.T) *fakePrimary {
	t.Helper()
	p := &fakePrimary{answers: map[string]fakeAnswer{
		"GET /links":   {http.StatusOK, "[]"},
		"POST /status": {http.StatusNoContent, ""},
	}}
	p.Server = httptest.NewServer(http.HandlerFunc(p.serve))
	t.Cleanup(p.Close)
	return p
}

func (p *fakePrimary) serve(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	route := r.Method + " " + r.URL.Path
	p.mu.Lock()
	p.requests = append(p.requests, fakeRequest{route, r.Header.Get("Authorization"), body})
	answer, known := p.answers[route]
	onStatus, onConnect := p.onStatus, p.onConnect
	p.mu.Unlock()
	if route == "POST /status" && onStatus != nil {
		onStatus()
	}
	if strings.HasSuffix(route, "/connect") && onConnect != nil {
		onConnect()
	}
	if !known {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(answer.code)
	io.WriteString(w, answer.body)
}

func (p *fakePrimary) answer(route string, code int, body string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.answers[route] = fakeAnswer{code, body}
}

// list sets what GET /links answers.
func (p *fakePrimary) list(t *testing.T, links ...DesiredLink) {
	t.Helper()
	body, err := json.Marshal(append([]DesiredLink{}, links...))
	if err != nil {
		t.Fatal(err)
	}
	p.answer("GET /links", http.StatusOK, string(body))
}

// holdStatus makes every POST /status run hold before it is answered.
func (p *fakePrimary) holdStatus(hold func()) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.onStatus = hold
}

// sent returns the requests of one route, in the order they came.
func (p *fakePrimary) sent(route string) []fakeRequest {
	p.mu.Lock()
	defer p.mu.Unlock()
	var sent []fakeRequest
	for _, request := range p.requests {
		if request.route == route {
			sent = append(sent, request)
		}
	}
	return sent
}

// fixture reads a file of the contract the Python listener's tests read too.
func fixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func jsonValue(t *testing.T, raw []byte) any {
	t.Helper()
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatalf("not JSON: %v: %s", err, raw)
	}
	return value
}

func ptr(s string) *string { return &s }

func TestPrimarySendsBearerAndParsesLinks(t *testing.T) {
	primary := newFakePrimary(t)
	primary.answer("GET /links", http.StatusOK, string(fixture(t, "links.json")))

	links, err := NewPrimary(primary.URL, "tok").Links(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	sent := primary.sent("GET /links")
	if len(sent) != 1 || sent[0].authorization != "Bearer tok" {
		t.Fatalf("requests = %+v, want one with Authorization: Bearer tok", sent)
	}
	want := []DesiredLink{
		{ID: idA, Hostname: "mind-bas", Desired: "on", AllowedLogin: ptr("bas@github"), ClosedBefore: 1795000000.5, LoginAfter: 1795000100.25},
		{ID: idB, Hostname: "code", Desired: "removing", AllowedLogin: nil, ClosedBefore: 1795000200.5, LoginAfter: 1795000200.5},
	}
	if !reflect.DeepEqual(links, want) {
		t.Fatalf("links = %+v, want %+v", links, want)
	}
}

func TestLinksAnswerThatIsNotAListIsAnError(t *testing.T) {
	for _, answer := range []fakeAnswer{
		{http.StatusInternalServerError, `{"detail": "x"}`},
		{http.StatusUnauthorized, `{"detail": "Unauthorized"}`},
		// A list is the primary's word only as its 200: read as no links, this one would stop every node.
		{http.StatusBadGateway, "[]"},
		{http.StatusOK, `{"detail": "x"}`},
		{http.StatusOK, "not json"},
		// JSON null decodes into a list without an error, and is none.
		{http.StatusOK, "null"},
	} {
		primary := newFakePrimary(t)
		primary.answer("GET /links", answer.code, answer.body)

		links, err := NewPrimary(primary.URL, "tok").Links(context.Background())

		if err == nil {
			t.Errorf("answer %+v: links = %+v, want an error", answer, links)
		}
	}
}

func TestStatusBodyIsTheContracts(t *testing.T) {
	primary := newFakePrimary(t)
	client := NewPrimary(primary.URL, "tok")
	links := map[string]LinkStatus{idA: {
		State:           "running",
		LoginURL:        ptr("https://login.tailscale.com/a/1c0ffee"),
		Tailnet:         ptr("tail1234.ts.net"),
		DNSName:         ptr("mind-bas.tail1234.ts.net"),
		LoginName:       ptr("bas@github"),
		KeyExpiry:       ptr("2027-05-19T12:00:00Z"),
		LastError:       ptr("scope"),
		OpenConnections: 2,
	}}

	if err := client.PostStatus(context.Background(), "auth_key", links); err != nil {
		t.Fatal(err)
	}
	if err := client.PostStatus(context.Background(), "login", map[string]LinkStatus{idB: {State: "removed"}}); err != nil {
		t.Fatal(err)
	}
	// A gateway that holds no node reports an object without links, never null.
	if err := client.PostStatus(context.Background(), "login", nil); err != nil {
		t.Fatal(err)
	}

	sent := primary.sent("POST /status")
	if len(sent) != 3 || sent[0].authorization != "Bearer tok" {
		t.Fatalf("requests = %+v, want three with Authorization: Bearer tok", sent)
	}
	if got, want := jsonValue(t, sent[0].body), jsonValue(t, fixture(t, "status.json")); !reflect.DeepEqual(got, want) {
		t.Errorf("status body = %s, want testdata/status.json", sent[0].body)
	}
	removed := `{"enrollment": "login", "links": {"fedcba9876543210": {"state": "removed", "login_url": null,
		"tailnet": null, "dns_name": null, "login_name": null, "key_expiry": null, "open_connections": 0,
		"last_error": null}}}`
	if got, want := jsonValue(t, sent[1].body), jsonValue(t, []byte(removed)); !reflect.DeepEqual(got, want) {
		t.Errorf("status body = %s, want %s", sent[1].body, removed)
	}
	empty := `{"enrollment": "login", "links": {}}`
	if got, want := jsonValue(t, sent[2].body), jsonValue(t, []byte(empty)); !reflect.DeepEqual(got, want) {
		t.Errorf("status body = %s, want %s", sent[2].body, empty)
	}
}

func TestStatusThatIsNotTakenIsAnError(t *testing.T) {
	primary := newFakePrimary(t)
	primary.answer("POST /status", http.StatusServiceUnavailable, `{"detail": "Tailnet SSH is off."}`)

	err := NewPrimary(primary.URL, "tok").PostStatus(context.Background(), "login", nil)

	if err == nil {
		t.Fatal("PostStatus returned no error for a 503")
	}
}

func TestConnectAnswersTheTarget(t *testing.T) {
	primary := newFakePrimary(t)
	primary.answer("POST /links/"+idA+"/connect", http.StatusOK, `{"host": "10.0.0.5", "port": 2222}`)

	host, port, err := NewPrimary(primary.URL, "tok").Connect(context.Background(), idA, "bas@github", "laptop")
	if err != nil {
		t.Fatal(err)
	}

	if host != "10.0.0.5" || port != 2222 {
		t.Errorf("target = %s:%d, want 10.0.0.5:2222", host, port)
	}
	sent := primary.sent("POST /links/" + idA + "/connect")
	if len(sent) != 1 || sent[0].authorization != "Bearer tok" {
		t.Fatalf("requests = %+v, want one with Authorization: Bearer tok", sent)
	}
	if got, want := jsonValue(t, sent[0].body), jsonValue(t, fixture(t, "connect.json")); !reflect.DeepEqual(got, want) {
		t.Errorf("connect body = %s, want testdata/connect.json", sent[0].body)
	}
}

func TestConnectRefusalCarriesDetail(t *testing.T) {
	for _, refusal := range []struct {
		answer fakeAnswer
		detail string
	}{
		{fakeAnswer{http.StatusConflict, `{"detail":"scope"}`}, "scope"},
		{fakeAnswer{http.StatusNotFound, `{"detail":"No such link."}`}, "No such link."},
		{fakeAnswer{http.StatusServiceUnavailable, `{"detail":"Tailnet SSH is off."}`}, "Tailnet SSH is off."},
		// An answer that is not the contract's still refuses, and says with what status.
		{fakeAnswer{http.StatusBadGateway, "<html>"}, "502 Bad Gateway"},
		{fakeAnswer{http.StatusUnprocessableEntity, `{"detail":[{"type":"missing"}]}`}, "422 Unprocessable Entity"},
	} {
		primary := newFakePrimary(t)
		primary.answer("POST /links/"+idA+"/connect", refusal.answer.code, refusal.answer.body)

		_, _, err := NewPrimary(primary.URL, "tok").Connect(context.Background(), idA, "bas@github", "laptop")

		var refused *RefusedError
		if !errors.As(err, &refused) || refused.Detail != refusal.detail {
			t.Errorf("answer %+v: err = %v, want a RefusedError with detail %q", refusal.answer, err, refusal.detail)
		}
	}
}

func TestConnectAnswerWithoutATargetIsAnError(t *testing.T) {
	for _, body := range []string{
		"{}",
		"null",
		"not json",
		`{"host": "10.0.0.5"}`,
		`{"port": 2222}`,
		`{"host": "", "port": 2222}`,
		`{"host": "10.0.0.5", "port": 0}`,
		`{"host": "10.0.0.5", "port": 65536}`,
		`{"host": "10.0.0.5", "port": "2222"}`,
	} {
		primary := newFakePrimary(t)
		primary.answer("POST /links/"+idA+"/connect", http.StatusOK, body)

		host, port, err := NewPrimary(primary.URL, "tok").Connect(context.Background(), idA, "bas@github", "laptop")

		// The primary did not refuse, so its words are no link's last error.
		var refused *RefusedError
		if err == nil || errors.As(err, &refused) {
			t.Errorf("answer %s: target = %q:%d, err = %v, want an error that is no RefusedError", body, host, port, err)
		}
	}
}

func TestPrimaryThatDoesNotAnswerIsNotARefusal(t *testing.T) {
	primary := newFakePrimary(t)
	primary.Close()

	_, _, err := NewPrimary(primary.URL, "tok").Connect(context.Background(), idA, "bas@github", "laptop")

	var refused *RefusedError
	if err == nil || errors.As(err, &refused) {
		t.Fatalf("err = %v, want an error that is no RefusedError", err)
	}
}

func TestPrimaryIsNeverReachedThroughAProxy(t *testing.T) {
	client := NewPrimary("http://primary.invalid", "tok").(*httpPrimary)

	if transport := client.client.Transport.(*http.Transport); transport.Proxy != nil {
		t.Error("the primary's client takes a proxy, which would be sent the gateway's token")
	}
}

// deadlines records how long each request may take, and answers every one with an empty 204.
type deadlines struct{ allowed map[string]time.Duration }

func (d deadlines) RoundTrip(r *http.Request) (*http.Response, error) {
	deadline, _ := r.Context().Deadline()
	d.allowed[r.Method+" "+r.URL.Path] = time.Until(deadline).Round(time.Second)
	return &http.Response{StatusCode: http.StatusNoContent, Body: io.NopCloser(strings.NewReader(""))}, nil
}

func TestConnectMayTake610SecondsAndTheOthersTen(t *testing.T) {
	client := NewPrimary("http://primary.invalid", "tok").(*httpPrimary)
	seen := deadlines{map[string]time.Duration{}}
	client.client.Transport = seen

	client.Links(context.Background())
	client.PostStatus(context.Background(), "login", nil)
	client.Connect(context.Background(), idA, "bas@github", "laptop")

	want := map[string]time.Duration{
		"GET /links":                      10 * time.Second,
		"POST /status":                    10 * time.Second,
		"POST /links/" + idA + "/connect": 610 * time.Second,
	}
	if !reflect.DeepEqual(seen.allowed, want) {
		t.Fatalf("timeouts = %v, want %v", seen.allowed, want)
	}
}
