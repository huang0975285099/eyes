package recording

import (
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
