package analysis

import (
	"testing"
	"time"
)

func TestDecodeLiveFrameConfig(t *testing.T) {
	for _, test := range []struct {
		raw  string
		want LiveFrameConfig
	}{
		{`{"interval_minutes":10,"frames_per_interval":1}`, LiveFrameConfig{10, 1, 24}},
		{`{"interval_minutes":10,"frames_per_interval":3,"retain_hours":8}`, LiveFrameConfig{10, 3, 8}},
		{`{"frames_per_minute":60}`, LiveFrameConfig{1, 60, 24}},
		{`{"interval_minutes":1,"frames_per_interval":61}`, LiveFrameConfig{1, 2, 24}},
		{"invalid", LiveFrameConfig{1, 2, 24}},
	} {
		if got := DecodeLiveFrameConfig(test.raw); got != test.want {
			t.Fatalf("DecodeLiveFrameConfig(%q) = %#v, want %#v", test.raw, got, test.want)
		}
	}
}

func TestLiveFrameSchedule(t *testing.T) {
	now := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	scheduled, _ := liveFrameSchedule(7, 10, 1, now)
	if interval := now.Sub(scheduled); interval < 0 || interval >= 10*time.Minute {
		t.Fatalf("scheduled time %v is outside the current 10-minute slot", scheduled)
	}
	if _, due := liveFrameSchedule(7, 10, 1, scheduled.Add(10*time.Second)); !due {
		t.Fatal("expected source to be due during the scheduling window")
	}
	if _, due := liveFrameSchedule(7, 10, 1, scheduled.Add(13*time.Second)); due {
		t.Fatal("expected source not to be due outside the scheduling window")
	}

	other, _ := liveFrameSchedule(8, 10, 1, now)
	if scheduled.Equal(other) {
		t.Fatal("different sources should be staggered")
	}
}

func TestLiveFrameJobCutoff(t *testing.T) {
	now := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	want := now.Add(-2 * time.Minute)
	if got := LiveFrameJobCutoff(now); !got.Equal(want) {
		t.Fatalf("LiveFrameJobCutoff() = %v, want %v", got, want)
	}
}

func TestResetPendingLiveFrameSamplerJobsRequiresSourceScope(t *testing.T) {
	if err := ResetPendingLiveFrameSamplerJobs(); err != nil {
		t.Fatalf("empty source scope should be a no-op: %v", err)
	}
}
