package analysis

import (
	"encoding/json"
	"fmt"
	"media-service/database"
	"media-service/models"
	"time"

	"gorm.io/gorm/clause"
)

const (
	AlgorithmFrameSampler = "frame_sampler"
	JobInputSegment       = "recording_segment" // 仅用于清理旧版本任务
	JobInputLiveStream    = "live_stream"
	JobStatusPending      = "pending"
	JobStatusRunning      = "running"
	JobStatusSucceeded    = "succeeded"
	JobStatusFailed       = "failed"
)

var builtinAlgorithms = []models.AIAlgorithm{
	{
		Code: AlgorithmFrameSampler, Name: "实时流抽帧", InputMode: "live_stream", Enabled: true,
		Description:       "直接从SRS实时视频流按频率抽取JPEG图片，与录像开关无关。",
		DefaultConfigJSON: `{"frames_per_minute":2}`,
	},
	{
		Code: "fight", Name: "打架检测", InputMode: "video", Enabled: false,
		Description: "连续视频中的疑似打架行为检测（待接入模型）。",
	},
	{
		Code: "helmet", Name: "安全帽检测", InputMode: "frame", Enabled: false,
		Description: "人员是否按要求佩戴安全帽（待接入模型）。",
	},
	{
		Code: "fire", Name: "火灾检测", InputMode: "video", Enabled: false,
		Description: "火焰和烟雾异常检测（待接入模型）。",
	},
}

// InitializeCatalog updates platform-owned algorithm metadata. Segment-based
// frame-sampler jobs belonged to the old design and must never be claimed by
// the real-time sampler.
func InitializeCatalog() error {
	for _, algorithm := range builtinAlgorithms {
		if err := database.DB.Clauses(clause.OnConflict{
			Columns: []clause.Column{{Name: "code"}},
			DoUpdates: clause.AssignmentColumns([]string{
				"name", "description", "input_mode", "default_config_json", "updated_at",
			}),
		}).Create(&algorithm).Error; err != nil {
			return fmt.Errorf("初始化算法 %s 失败: %w", algorithm.Code, err)
		}
	}
	if err := database.DB.Where(
		"algorithm_code = ? AND input_type = ?", AlgorithmFrameSampler, JobInputSegment,
	).Delete(&models.AIJob{}).Error; err != nil {
		return fmt.Errorf("清理旧版录像抽帧任务失败: %w", err)
	}
	return nil
}

type configuredLiveSource struct {
	ID         uint   `gorm:"column:id"`
	StreamName string `gorm:"column:stream_name"`
	ConfigJSON string `gorm:"column:config_json"`
}

// EnqueueLiveFrameSamplerJobs creates at most one durable task for every due
// source/time slot. Sources are staggered across the interval to avoid all
// cameras opening an FFmpeg connection at the same instant.
func EnqueueLiveFrameSamplerJobs(activeStreams map[string]struct{}, now time.Time) error {
	if len(activeStreams) == 0 {
		return nil
	}
	var sources []configuredLiveSource
	if err := database.DB.Table("video_sources").
		Select("video_sources.id, video_sources.stream_name, video_analysis_rules.config_json").
		Joins("JOIN video_analysis_rules ON video_analysis_rules.video_source_id = video_sources.id").
		Where("video_sources.enabled = ? AND video_analysis_rules.algorithm_code = ? AND video_analysis_rules.enabled = ?",
			true, AlgorithmFrameSampler, true).
		Find(&sources).Error; err != nil {
		return fmt.Errorf("查询实时抽帧视频源失败: %w", err)
	}

	for _, source := range sources {
		if _, active := activeStreams[source.StreamName]; !active {
			continue
		}
		rate := framesPerMinute(source.ConfigJSON)
		scheduledAt, due := liveFrameSchedule(source.ID, rate, now)
		if !due {
			continue
		}
		job := models.AIJob{
			JobKey:        fmt.Sprintf("%s:live:%d:%d", AlgorithmFrameSampler, source.ID, scheduledAt.UnixNano()),
			AlgorithmCode: AlgorithmFrameSampler, InputType: JobInputLiveStream,
			InputRefID: source.ID, Status: JobStatusPending, MaxAttempts: 3,
			AvailableAt: now,
		}
		if err := database.DB.Clauses(clause.OnConflict{DoNothing: true}).Create(&job).Error; err != nil {
			return fmt.Errorf("创建实时抽帧任务失败 source=%d: %w", source.ID, err)
		}
	}
	return nil
}

// ResetPendingLiveFrameSamplerJobs removes not-yet-started slots after a rule
// change. The scheduler recreates only slots matching the current selection
// and frequency; running and completed results are preserved.
func ResetPendingLiveFrameSamplerJobs() error {
	return database.DB.Where(
		"algorithm_code = ? AND input_type = ? AND status = ?",
		AlgorithmFrameSampler, JobInputLiveStream, JobStatusPending,
	).Delete(&models.AIJob{}).Error
}

func framesPerMinute(raw string) int {
	config := struct {
		FramesPerMinute int `json:"frames_per_minute"`
	}{FramesPerMinute: 2}
	_ = json.Unmarshal([]byte(raw), &config)
	if config.FramesPerMinute < 1 {
		return 1
	}
	if config.FramesPerMinute > 60 {
		return 60
	}
	return config.FramesPerMinute
}

func liveFrameSchedule(sourceID uint, rate int, now time.Time) (time.Time, bool) {
	if rate < 1 {
		rate = 1
	}
	if rate > 60 {
		rate = 60
	}
	interval := time.Minute / time.Duration(rate)
	intervalNS := interval.Nanoseconds()
	offsetNS := (int64(sourceID) * int64(7919*time.Millisecond)) % intervalNS
	slot := (now.UnixNano() - offsetNS) / intervalNS
	scheduled := time.Unix(0, slot*intervalNS+offsetNS)
	age := now.Sub(scheduled)
	return scheduled, age >= 0 && age < 2*time.Second
}
