package main

import (
	"context"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tailcfg"
)

// enrolled is the status of a node in a tailnet, as tsnet gives it.
func enrolled() *ipnstate.Status {
	expiry := time.Date(2027, 5, 19, 14, 0, 0, 0, time.FixedZone("CEST", 2*60*60))
	return &ipnstate.Status{
		CurrentTailnet: &ipnstate.TailnetStatus{Name: "bas@github", MagicDNSSuffix: "tail1234.ts.net"},
		Self:           &ipnstate.PeerStatus{DNSName: "mind-bas.tail1234.ts.net.", UserID: 7, KeyExpiry: &expiry},
		User:           map[tailcfg.UserID]tailcfg.UserProfile{7: {LoginName: "bas@github"}},
	}
}

func TestObserveMapsTsnetStatesToTheContracts(t *testing.T) {
	url := ptr("https://login.tailscale.com/a/1c0ffee")
	for state, want := range map[ipn.State]NodeObserved{
		ipn.NoState:          {State: "needs_login", LoginURL: url},
		ipn.NeedsLogin:       {State: "needs_login", LoginURL: url},
		ipn.Starting:         {State: "needs_login", LoginURL: url},
		ipn.Stopped:          {State: "needs_login", LoginURL: url},
		ipn.NeedsMachineAuth: {State: "needs_approval"},
		ipn.Running:          {State: "running"},
	} {
		if got := observe(state, url, &ipnstate.Status{}, &ipnstate.TailnetLockStatus{}); !reflect.DeepEqual(got, want) {
			t.Errorf("a node that is %v is seen as %+v, want %+v", state, got, want)
		}
	}
}

func TestObserveSeesANodeItsTailnetsLockKeepsOutAsWaitingForApproval(t *testing.T) {
	for name, test := range map[string]struct {
		lock ipnstate.TailnetLockStatus
		want string
	}{
		"a tailnet without a lock":             {ipnstate.TailnetLockStatus{}, "running"},
		"a lock that has the node's signature": {ipnstate.TailnetLockStatus{Enabled: true, NodeKeySigned: true}, "running"},
		"a lock without the node's signature":  {ipnstate.TailnetLockStatus{Enabled: true}, "needs_approval"},
	} {
		if got := observe(ipn.Running, nil, enrolled(), &test.lock); got.State != test.want || got.DNSName == nil {
			t.Errorf("a running node in %s is seen as %+v, want %s with its names", name, got, test.want)
		}
	}
	// Only a node in a tailnet is kept out by its lock: one that needs a login still needs it.
	unsigned := &ipnstate.TailnetLockStatus{Enabled: true}
	if got := observe(ipn.NeedsLogin, nil, &ipnstate.Status{}, unsigned); got.State != "needs_login" {
		t.Errorf("a node that needs a login is seen as %+v under a lock, want needs_login", got)
	}
}

func TestObserveReadsTheTailnetFromTheStatus(t *testing.T) {
	got := observe(ipn.Running, nil, enrolled(), &ipnstate.TailnetLockStatus{})

	want := NodeObserved{
		State:     "running",
		Tailnet:   ptr("tail1234.ts.net"),
		DNSName:   ptr("mind-bas.tail1234.ts.net"),
		LoginName: ptr("bas@github"),
		KeyExpiry: ptr("2027-05-19T12:00:00Z"),
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("the node is seen as %+v, want %+v", got, want)
	}
}

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

// A node of the real factory, with no login server to reach: it starts, listens and needs a login, keeps its
// state where it was told to, and closes.
func TestTsnetNodeStartsWithoutALoginServer(t *testing.T) {
	dir := filepath.Join(t.TempDir(), idA)

	node, err := NewTsnetNode("http://127.0.0.1:1", "", []string{"tag:mindroom"})(dir, "mind-bas")
	if err != nil {
		t.Fatal(err)
	}

	if got := node.Observed(); got.State != "needs_login" || got.DNSName != nil {
		t.Errorf("a node no tailnet has is seen as %+v, want one that needs a login", got)
	}
	if node.Listener() == nil || !exists(t, filepath.Join(dir, "tailscaled.state")) {
		t.Error("the node has no listener or no state in its directory")
	}
	// The watch asks for the lock's status on every change and takes no answer for no change, so the node has to
	// answer it.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if lock, err := node.(*tsnetNode).client.TailnetLockStatus(ctx); err != nil || lock.Enabled {
		t.Errorf("the node's lock status is %+v, %v, want one of no lock", lock, err)
	}
	if err := node.Close(); err != nil {
		t.Errorf("closing the node: %v", err)
	}
	if _, err := node.Listener().Accept(); err == nil {
		t.Error("the closed node still accepts connections")
	}
}
