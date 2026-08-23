package analysis

import (
	"fmt"
	"log"
	"media-service/database"
	"media-service/models"
	"time"

	"gorm.io/gorm/clause"
)

const (
	AlgorithmFrameSampler = "frame_sampler"
	JobInputSegment       = "recording_segment"
	JobStatusPending      = "pending"
	JobStatusRunning      = "running"
	JobStatusSucceeded    = "succeeded"
	JobStatusFailed       = "failed"
)

var builtinAlgorithms = []models.AIAlgorithm{
	{
		Code: AlgorithmFrameSampler, Name: "录像抽帧", InputMode: "segment", Enabled: true,
		Description:       "从已完成的录像片段抽取代表性图片，供人工查看。",
		DefaultConfigJSON: `{"min_duration_seconds":30,"interval_seconds":300,"minimum_frames":2}`,
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

// InitializeCatalog upserts platform-owned algorithm metadata and creates
// frame-sampler jobs for existing local segments. Existing operator settings
// are preserved: only descriptive fields are updated on a conflict.
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

	return BackfillFrameSamplerJobs()
}

// EnqueueFrameSampler creates an idempotent job for a local recording segment.
func EnqueueFrameSampler(segment models.RecordingSegment) error {
	if segment.ID == 0 || segment.Storage != "local" {
		return nil
	}
	now := time.Now()
	job := models.AIJob{
		JobKey:        frameSamplerJobKey(segment.ID),
		AlgorithmCode: AlgorithmFrameSampler,
		InputType:     JobInputSegment,
		InputRefID:    segment.ID,
		Status:        JobStatusPending,
		MaxAttempts:   3,
		AvailableAt:   now,
	}
	return database.DB.Clauses(clause.OnConflict{DoNothing: true}).Create(&job).Error
}

// BackfillFrameSamplerJobs makes the extraction move safe for already indexed
// recordings. It intentionally keeps completed legacy frames: AIService will
// reuse the existing JPEG and merely report it idempotently.
func BackfillFrameSamplerJobs() error {
	const batchSize = 500
	var lastID uint
	var created int64
	for {
		var segments []models.RecordingSegment
		if err := database.DB.Where("storage = ? AND id > ?", "local", lastID).
			Order("id ASC").Limit(batchSize).Find(&segments).Error; err != nil {
			return fmt.Errorf("查询待补建抽帧任务的录像失败: %w", err)
		}
		if len(segments) == 0 {
			break
		}

		jobs := make([]models.AIJob, 0, len(segments))
		now := time.Now()
		for _, segment := range segments {
			jobs = append(jobs, models.AIJob{
				JobKey:        frameSamplerJobKey(segment.ID),
				AlgorithmCode: AlgorithmFrameSampler,
				InputType:     JobInputSegment,
				InputRefID:    segment.ID,
				Status:        JobStatusPending,
				MaxAttempts:   3,
				AvailableAt:   now,
			})
			lastID = segment.ID
		}
		result := database.DB.Clauses(clause.OnConflict{DoNothing: true}).Create(&jobs)
		if result.Error != nil {
			return fmt.Errorf("补建抽帧任务失败: %w", result.Error)
		}
		created += result.RowsAffected
	}
	if created > 0 {
		log.Printf("[analysis] 已为历史录像补建抽帧任务 %d 条", created)
	}
	return nil
}

func frameSamplerJobKey(segmentID uint) string {
	return fmt.Sprintf("%s:segment:%d", AlgorithmFrameSampler, segmentID)
}
