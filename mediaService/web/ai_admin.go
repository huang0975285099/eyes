package web

import (
	"encoding/json"
	"fmt"
	"media-service/analysis"
	"media-service/database"
	"media-service/models"
	"net/http"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

type frameSamplerRuleRow struct {
	VideoSourceID  uint           `json:"video_source_id"`
	StreamName     string         `json:"stream_name"`
	DisplayName    string         `json:"display_name"`
	SourceType     string         `json:"source_type"`
	SourceID       string         `json:"source_id"`
	MAC            string         `json:"mac"`
	Active         bool           `json:"active"`
	Enabled        bool           `json:"enabled"`
	Config         map[string]any `json:"config"`
	FrameCount     int64          `json:"frame_count"`
	LastCapturedAt *time.Time     `json:"last_captured_at,omitempty"`
}

type frameSamplerRuleUpdate struct {
	AlgorithmCode    string `json:"algorithm_code"`
	EnabledSourceIDs []uint `json:"enabled_source_ids"`
	Config           struct {
		FramesPerMinute int `json:"frames_per_minute"`
	} `json:"config"`
}

// handleAIAnalysisRules is the operator-facing configuration API used by the
// AIService dashboard. MediaService remains the owner of persistent rules.
func (s *Server) handleAIAnalysisRules(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		s.listFrameSamplerRules(w)
	case http.MethodPut:
		s.updateFrameSamplerRules(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) listFrameSamplerRules(w http.ResponseWriter) {
	var sources []models.VideoSource
	if err := database.DB.Order("display_name, id").Find(&sources).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询视频源失败"})
		return
	}

	var rules []models.VideoAnalysisRule
	if err := database.DB.Where("algorithm_code = ?", analysis.AlgorithmFrameSampler).Find(&rules).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询分析规则失败"})
		return
	}
	ruleBySource := make(map[uint]models.VideoAnalysisRule, len(rules))
	for _, rule := range rules {
		ruleBySource[rule.VideoSourceID] = rule
	}

	type frameAggregate struct {
		StreamName     string     `gorm:"column:stream_name"`
		FrameCount     int64      `gorm:"column:frame_count"`
		LastCapturedAt *time.Time `gorm:"column:last_captured_at"`
	}
	var aggregates []frameAggregate
	if err := database.DB.Model(&models.RecordingFrame{}).
		Select("stream_name, COUNT(*) AS frame_count, MAX(captured_at) AS last_captured_at").
		Where("segment_id = 0").
		Group("stream_name").Scan(&aggregates).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "统计抽帧结果失败"})
		return
	}
	frameByStream := make(map[string]frameAggregate, len(aggregates))
	for _, aggregate := range aggregates {
		frameByStream[aggregate.StreamName] = aggregate
	}

	activeSet := make(map[string]bool)
	for _, stream := range s.fetchSRSStreams() {
		activeSet[stream.Name] = stream.Publish.Active
	}

	rows := make([]frameSamplerRuleRow, 0, len(sources))
	for _, source := range sources {
		config := map[string]any{"frames_per_minute": 2}
		rule := ruleBySource[source.ID]
		if strings.TrimSpace(rule.ConfigJSON) != "" {
			_ = json.Unmarshal([]byte(rule.ConfigJSON), &config)
		}
		aggregate := frameByStream[source.StreamName]
		rows = append(rows, frameSamplerRuleRow{
			VideoSourceID: source.ID, StreamName: source.StreamName,
			DisplayName: source.DisplayName, SourceType: source.SourceType,
			SourceID: source.SourceID, MAC: source.MAC, Active: activeSet[source.StreamName],
			Enabled: rule.Enabled, Config: config, FrameCount: aggregate.FrameCount,
			LastCapturedAt: aggregate.LastCapturedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"algorithm": map[string]any{
			"code": analysis.AlgorithmFrameSampler, "name": "实时流抽帧",
			"description": "按视频源直接从SRS实时流抽取JPEG图片，与录像开关无关。",
		},
		"sources": rows,
	})
}

func (s *Server) updateFrameSamplerRules(w http.ResponseWriter, r *http.Request) {
	var req frameSamplerRuleUpdate
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.AlgorithmCode != analysis.AlgorithmFrameSampler {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "当前仅支持配置frame_sampler"})
		return
	}
	if req.Config.FramesPerMinute < 1 || req.Config.FramesPerMinute > 60 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "frames_per_minute必须在1到60之间"})
		return
	}

	var sources []models.VideoSource
	if err := database.DB.Find(&sources).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询视频源失败"})
		return
	}
	selected := make(map[uint]struct{}, len(req.EnabledSourceIDs))
	for _, id := range req.EnabledSourceIDs {
		selected[id] = struct{}{}
	}
	known := make(map[uint]struct{}, len(sources))
	for _, source := range sources {
		known[source.ID] = struct{}{}
	}
	for id := range selected {
		if _, ok := known[id]; !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": fmt.Sprintf("视频源%d不存在", id)})
			return
		}
	}

	configJSON, _ := json.Marshal(map[string]int{"frames_per_minute": req.Config.FramesPerMinute})
	// Use individual upserts inside one GORM transaction so disabled sources are
	// persisted too; future video sources remain disabled until explicitly selected.
	if err := database.DB.Transaction(func(tx *gorm.DB) error {
		for _, source := range sources {
			_, enabled := selected[source.ID]
			rule := models.VideoAnalysisRule{
				VideoSourceID: source.ID, AlgorithmCode: analysis.AlgorithmFrameSampler,
				Enabled: enabled, ConfigJSON: string(configJSON),
			}
			if err := tx.Clauses(clause.OnConflict{
				Columns:   []clause.Column{{Name: "video_source_id"}, {Name: "algorithm_code"}},
				DoUpdates: clause.AssignmentColumns([]string{"enabled", "config_json", "updated_at"}),
			}).Create(&rule).Error; err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "保存抽帧规则失败"})
		return
	}
	if err := analysis.ResetPendingLiveFrameSamplerJobs(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "规则已保存，但清理旧调度任务失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "enabled_source_count": len(selected),
		"frames_per_minute": req.Config.FramesPerMinute,
	})
}
