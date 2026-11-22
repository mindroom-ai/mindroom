package main

import (
	"context"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/health"
	"tailscale.com/ipn"
	"tailscale.com/tailcfg"
)

func TestPeerOfATaggedDeviceHasNoLogin(t *testing.T) {
	person := &apitype.WhoIsResponse{
		Node:        &tailcfg.Node{Name: "laptop.tail1234.ts.net.", ComputedName: "laptop"},
		UserProfile: &tailcfg.UserProfile{LoginName: "bas@github"},
	}
	tagged := &apitype.WhoIsResponse{
		Node:        &tailcfg.Node{Name: "ci.tail1234.ts.net.", Tags: []string{"tag:ci"}},
		UserProfile: &tailcfg.UserProfile{LoginName: "tagged-devices"},
	}

	whoIs := func(response *apitype.WhoIsResponse) (string, string, error) {
		n := tsnetNode{whoIs: func(context.Context, string) (*apitype.WhoIsResponse, error) { return response, nil }}
		return n.WhoIs(context.Background(), "100.64.1.2:3456")
	}
	if login, node, _ := whoIs(person); login != "bas@github" || node != "laptop" {
		t.Errorf("a person's machine is %q of %q, want laptop of bas@github", node, login)
	}
	if login, node, _ := whoIs(tagged); login != "" || node != "ci.tail1234.ts.net" {
		t.Errorf("a tagged device is %q of %q, want ci.tail1234.ts.net of no login", node, login)
	}
	if login, node, _ := whoIs(&apitype.WhoIsResponse{UserProfile: person.UserProfile}); login != "" || node != "" {
		t.Errorf("an answer without a machine is %q of %q, want nobody", node, login)
	}
}

func TestTsnetNodeNeedsAnAuthKeyBeforeStarting(t *testing.T) {
	for _, key := range []string{"", " \t"} {
		factory, err := NewTsnetNode("", key, nil)
		if factory != nil || err == nil || !strings.Contains(err.Error(), "TAILNET_SSH_AUTH_KEY") {
			t.Fatalf("expected missing TAILNET_SSH_AUTH_KEY error, got %v", err)
		}
	}
}

func TestTsnetNodeStartsWithTrimmedEnrollmentKey(t *testing.T) {
	t.Setenv("TS_NO_LOGS_NO_SUPPORT", "true")
	for _, name := range []string{"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"} {
		t.Setenv(name, "")
	}
	t.Setenv("TS_LOGS_DIR", t.TempDir())
	const loginServer = "http://127.0.0.1:1"
	dir, tags := t.TempDir(), []string{"tag:mindroom"}
	factory, err := NewTsnetNode(loginServer, " \toperator-key\n", tags)
	if err != nil {
		t.Fatal(err)
	}
	node, err := factory(dir, "mind-test")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { node.Close() })
	server := node.(*tsnetNode).Server
	if server.ControlURL != loginServer || server.Dir != dir || server.Hostname != "mind-test" ||
		server.AuthKey != "operator-key" || server.Ephemeral || !reflect.DeepEqual(server.AdvertiseTags, tags) {
		t.Error("tsnet node did not get the expected persistent enrollment config")
	}
}

func TestNodeLoggerLogsStateAndErrorsWithoutSecrets(t *testing.T) {
	const authKey = "tskey-auth-secret-123"
	const loginURL = "https://headscale.example.org/register/secret-token"
	var lines []string
	logger := &nodeLogger{
		id:       "97c96139be590736",
		hostname: "v1-default-helper",
		authKey:  authKey,
		logf:     func(format string, args ...any) { lines = append(lines, fmt.Sprintf(format, args...)) },
	}

	state := func(s ipn.State) *ipn.State { return &s }
	str := func(s string) *string { return &s }

	logger.observe(ipn.Notify{
		State: state(ipn.Starting),
		Health: &health.State{Warnings: map[health.WarnableCode]health.UnhealthyState{
			"warming-up": {Severity: health.SeverityLow, Text: "Tailscale is starting"},
		}},
	})
	logger.observe(ipn.Notify{
		State:       state(ipn.NeedsLogin),
		BrowseToURL: str(loginURL),
		ErrMessage:  str("auth failed for " + authKey + " visit " + loginURL),
		Health: &health.State{Warnings: map[health.WarnableCode]health.UnhealthyState{
			"login-state": {
				Severity: health.SeverityMedium,
				Text:     "register request: http 500: requested tags [tag:mindroom-ssh] are invalid with " + authKey,
			},
		}},
	})
	// Duplicates do not spam logs.
	logger.observe(ipn.Notify{
		State:      state(ipn.NeedsLogin),
		ErrMessage: str("auth failed for " + authKey + " visit " + loginURL),
		Health: &health.State{Warnings: map[health.WarnableCode]health.UnhealthyState{
			"login-state": {
				Severity: health.SeverityMedium,
				Text:     "register request: http 500: requested tags [tag:mindroom-ssh] are invalid with " + authKey,
			},
		}},
	})
	// Reaching Running and healthy clears deduplication state.
	logger.observe(ipn.Notify{
		State:  state(ipn.Running),
		Health: &health.State{},
	})
	logger.observe(ipn.Notify{
		Health: &health.State{Warnings: map[health.WarnableCode]health.UnhealthyState{
			"not-in-map-poll": {Severity: health.SeverityMedium, Title: "Out of sync"},
		}},
	})

	want := []string{
		"link 97c96139be590736 (v1-default-helper): tsnet state: Starting",
		"link 97c96139be590736 (v1-default-helper): tsnet state: NeedsLogin",
		"link 97c96139be590736 (v1-default-helper): tsnet error: auth failed for [redacted] visit [redacted]",
		"link 97c96139be590736 (v1-default-helper): tsnet login-state: register request: http 500: requested tags [tag:mindroom-ssh] are invalid with [redacted]",
		"link 97c96139be590736 (v1-default-helper): tsnet state: Running",
		"link 97c96139be590736 (v1-default-helper): tsnet not-in-map-poll: Out of sync",
	}
	if !reflect.DeepEqual(lines, want) {
		t.Fatalf("got logs:\n%s\nwant:\n%s", strings.Join(lines, "\n"), strings.Join(want, "\n"))
	}
	for _, line := range lines {
		if strings.Contains(line, authKey) || strings.Contains(line, loginURL) {
			t.Fatalf("secret or login URL leaked in log line: %q", line)
		}
	}
}
