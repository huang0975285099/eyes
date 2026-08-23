package web

import (
	"path/filepath"
	"reflect"
	"testing"
)

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
