package main

import (
	"reflect"
	"strings"
	"testing"

	"tailscale.com/client/tailscale/apitype"
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

	if login, node := peer(person); login != "bas@github" || node != "laptop" {
		t.Errorf("a person's machine is %q of %q, want laptop of bas@github", node, login)
	}
	if login, node := peer(tagged); login != "" || node != "ci.tail1234.ts.net" {
		t.Errorf("a tagged device is %q of %q, want ci.tail1234.ts.net of no login", node, login)
	}
	if login, node := peer(&apitype.WhoIsResponse{UserProfile: person.UserProfile}); login != "" || node != "" {
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

func TestTsnetNodeLeavesDefaultControlURLToTsnet(t *testing.T) {
	for _, loginServer := range []string{"", "https://headscale.example"} {
		dir, tags := t.TempDir(), []string{"tag:mindroom"}
		server := tsnetServer(dir, "mind-bas", loginServer, "operator-key", tags)
		if server.ControlURL != loginServer || server.Dir != dir || server.Hostname != "mind-bas" ||
			server.AuthKey != "operator-key" || server.Ephemeral || !reflect.DeepEqual(server.AdvertiseTags, tags) {
			t.Error("tsnet node did not get the expected persistent enrollment config")
		}
	}
}
