package analysis

import (
	"testing"
	"time"
)

func TestFramesPerMinute(t *testing.T) {
	for _, test := range []struct {
		raw  string
		want int
	}{
		{`{"frames_per_minute":2}`, 2},
		{`{"frames_per_minute":60}`, 60},
		{`{"frames_per_minute":0}`, 1},
		{`{"frames_per_minute":99}`, 60},
		{"invalid", 2},
	} {
		if got := framesPerMinute(test.raw); got != test.want {
			t.Fatalf("framesPerMinute(%q) = %d, want %d", test.raw, got, test.want)
		}
	}
}

func TestLiveFrameSchedule(t *testing.T) {
	now := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	scheduled, _ := liveFrameSchedule(7, 2, now)
	if interval := now.Sub(scheduled); interval < 0 || interval >= 30*time.Second {
		t.Fatalf("scheduled time %v is outside the current 30-second slot", scheduled)
	}
	if _, due := liveFrameSchedule(7, 2, scheduled.Add(time.Second)); !due {
		t.Fatal("expected source to be due during the two-second scheduling window")
	}
	if _, due := liveFrameSchedule(7, 2, scheduled.Add(3*time.Second)); due {
		t.Fatal("expected source not to be due outside the scheduling window")
	}

	other, _ := liveFrameSchedule(8, 2, now)
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
