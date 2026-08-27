package recording

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRecordingExpiredUsesEndTimeAndHours(t *testing.T) {
	now := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	if recordingExpired(now.Add(-8*time.Hour), now, 8) {
		t.Fatal("segment exactly eight hours old should remain until it passes the cutoff")
	}
	if !recordingExpired(now.Add(-8*time.Hour-time.Second), now, 8) {
		t.Fatal("segment older than eight hours should expire")
	}
	if recordingExpired(now.Add(-24*time.Hour), now, 0) {
		t.Fatal("non-positive retention must not expire a segment")
	}
}

func TestDecodeEventRetainHours(t *testing.T) {
	if got := decodeEventRetainHours(`{"retain_hours":168}`, 720); got != 168 {
		t.Fatalf("decodeEventRetainHours() = %d, want 168", got)
	}
	for _, raw := range []string{"", `{}`, `{"retain_hours":0}`, `{"retain_hours":90000}`} {
		if got := decodeEventRetainHours(raw, 720); got != 720 {
			t.Fatalf("decodeEventRetainHours(%q) = %d, want fallback", raw, got)
		}
	}
}

func TestRemoveEvidenceFileStaysWithinEventRoot(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "_events")
	inside := filepath.Join(root, "camera-1", "event.jpg")
	outside := filepath.Join(base, "keep.jpg")
	if err := os.MkdirAll(filepath.Dir(inside), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(inside, []byte("inside"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(outside, []byte("outside"), 0o644); err != nil {
		t.Fatal(err)
	}
	removeEvidenceFile(root, inside)
	removeEvidenceFile(root, outside)
	if _, err := os.Stat(inside); !os.IsNotExist(err) {
		t.Fatal("inside evidence was not removed")
	}
	if _, err := os.Stat(outside); err != nil {
		t.Fatal("outside file must not be removed")
	}
}

func TestRetentionHoursForPathUsesSourceRule(t *testing.T) {
	root := filepath.Join("var", "recordings")
	filePath := filepath.Join(root, "camera-1", "2026-08-24", "segment.mp4")
	retention := map[string]int{"camera-1": 8}
	if got := retentionHoursForPath(root, filePath, retention, 48); got != 8 {
		t.Fatalf("retentionHoursForPath() = %d, want 8", got)
	}
	if got := retentionHoursForPath(root, filepath.Join(root, "camera-2", "segment.mp4"), retention, 48); got != 48 {
		t.Fatalf("default retention = %d, want 48", got)
	}
}
