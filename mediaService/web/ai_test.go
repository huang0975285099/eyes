package web

import (
	"path/filepath"
	"reflect"
	"testing"
)

func TestBuildInternalRTMPURL(t *testing.T) {
	for _, test := range []struct {
		host string
		want string
	}{
		{"srs:1935", "rtmp://srs:1935/live/camera-1"},
		{"rtmp://srs:1935/", "rtmp://srs:1935/live/camera-1"},
		{"", "rtmp://srs:1935/live/camera-1"},
	} {
		if got := buildInternalRTMPURL(test.host, "camera-1"); got != test.want {
			t.Fatalf("buildInternalRTMPURL(%q) = %q, want %q", test.host, got, test.want)
		}
	}
}

func TestBuildInternalHLSURL(t *testing.T) {
	if got := buildInternalHLSURL("http://srs:8080/", "camera-1"); got != "http://srs:8080/live/camera-1.m3u8" {
		t.Fatalf("unexpected HLS URL: %q", got)
	}
}

func TestCleanCapabilities(t *testing.T) {
	got := cleanCapabilities([]string{" frame_sampler ", "FIGHT", "fight", "", "helmet"})
	want := []string{"frame_sampler", "fight", "helmet"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("cleanCapabilities() = %#v, want %#v", got, want)
	}
}

func TestPathIsWithin(t *testing.T) {
	root := filepath.Join(t.TempDir(), "recordings", "_frames")
	inside := filepath.Join(root, "camera-1", "frame.jpg")
	outside := filepath.Join(filepath.Dir(root), "segment.mp4")
	if !pathIsWithin(root, inside) {
		t.Fatalf("expected %q to be inside %q", inside, root)
	}
	if pathIsWithin(root, outside) {
		t.Fatalf("expected %q to be outside %q", outside, root)
	}
}
