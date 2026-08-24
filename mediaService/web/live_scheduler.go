package web

import (
	"log"
	"media-service/analysis"
	"time"
)

// runLiveFrameScheduler only schedules sources that SRS currently reports as
// publishing. Recording state is deliberately not consulted: real-time frame
// sampling and MP4 recording are independent features.
func (s *Server) runLiveFrameScheduler() {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()

	active := make(map[string]struct{})
	nextStreamRefresh := time.Time{}
	for now := time.Now(); ; now = <-ticker.C {
		if !now.Before(nextStreamRefresh) {
			active = make(map[string]struct{})
			for _, stream := range s.fetchSRSStreams() {
				active[stream.Name] = struct{}{}
			}
			nextStreamRefresh = now.Add(10 * time.Second)
		}
		if err := analysis.EnqueueLiveFrameSamplerJobs(active, now); err != nil {
			log.Printf("[analysis] 调度实时流抽帧失败: %v", err)
		}
	}
}
