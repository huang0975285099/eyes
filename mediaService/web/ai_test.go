package web

import (
	"os"
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

func TestValidateEventFile(t *testing.T) {
	root := filepath.Join(t.TempDir(), "recordings", "_events")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	snapshot := filepath.Join(root, "camera-1", "event.jpg")
	if err := os.MkdirAll(filepath.Dir(snapshot), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(snapshot, []byte("jpeg"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := validateEventFile(root, snapshot, []string{".jpg"}, true); err != nil {
		t.Fatalf("expected valid event file: %v", err)
	}
	if err := validateEventFile(root, filepath.Join(filepath.Dir(root), "outside.jpg"), []string{".jpg"}, true); err == nil {
		t.Fatal("expected outside path to be rejected")
	}
	if err := validateEventFile(root, "", []string{".mp4"}, false); err != nil {
		t.Fatalf("optional empty clip should be accepted: %v", err)
	}
}

func TestChooseRealtimeWorkerIsStableAndRequiresCapabilities(t *testing.T) {
	workers := map[string]map[string]struct{}{
		"worker-a": capabilitySet([]string{"quality", "intrusion"}),
		"worker-b": capabilitySet([]string{"quality", "intrusion"}),
		"worker-c": capabilitySet([]string{"quality"}),
	}
	first := chooseRealtimeWorker("camera-1", []string{"quality", "intrusion"}, workers)
	second := chooseRealtimeWorker("camera-1", []string{"quality", "intrusion"}, workers)
	if first != second {
		t.Fatalf("assignment is not stable: %q != %q", first, second)
	}
	if first == "worker-c" || first == "" {
		t.Fatalf("assigned an ineligible worker: %q", first)
	}
	if got := chooseRealtimeWorker("camera-1", []string{"fire"}, workers); got != "" {
		t.Fatalf("expected no eligible worker, got %q", got)
	}
}
