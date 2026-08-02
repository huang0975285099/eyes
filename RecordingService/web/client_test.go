package web

import "testing"

func TestValidMAC(t *testing.T) {
	for _, value := range []string{"d8:5e:d3:9f:2a:17", "00:11:22:33:44:55"} {
		if !validMAC(value) {
			t.Fatalf("expected valid MAC: %s", value)
		}
	}
	for _, value := range []string{"", "not-a-mac", "00:11:22:33:44"} {
		if validMAC(value) {
			t.Fatalf("expected invalid MAC: %s", value)
		}
	}
}
