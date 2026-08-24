package web

import (
	"encoding/json"
	"fmt"
	"media-service/database"
	"media-service/models"
	"media-service/streamsource"
	"net/http"

	"gorm.io/gorm/clause"
)

type publishConfigRequest struct {
	MAC         string `json:"mac"`
	SourceType  string `json:"source_type"`
	SourceID    string `json:"source_id"`
	DisplayName string `json:"display_name"`
}

func (s *Server) handlePublishConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.PublicRTMPHost == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"message": "推流服务未配置"})
		return
	}
	defer r.Body.Close()
	var input publishConfigRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<10)).Decode(&input); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "请求数据无效"})
		return
	}
	mac, sourceType, sourceID, displayName, err := streamsource.Normalize(
		input.MAC, input.SourceType, input.SourceID, input.DisplayName,
	)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "视频源参数无效: " + err.Error()})
		return
	}
	streamName := streamsource.Name(mac, sourceType, sourceID)
	source := models.VideoSource{
		MAC: mac, SourceType: sourceType, SourceID: sourceID,
		DisplayName: displayName, PublishMode: "app", Enabled: true, StreamName: streamName,
	}
	if err := database.DB.Clauses(clause.OnConflict{
		Columns: []clause.Column{{Name: "stream_name"}},
		DoUpdates: clause.AssignmentColumns([]string{
			"mac", "source_type", "source_id", "display_name", "publish_mode", "enabled", "updated_at",
		}),
	}).Create(&source).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "登记视频源失败"})
		return
	}
	rtmpURL := fmt.Sprintf("rtmp://%s/live/%s", s.PublicRTMPHost, streamName)
	playbackURL := ""
	if s.SRSHttpHost != "" {
		playbackURL = fmt.Sprintf("http://%s/live/%s.flv", s.SRSHttpHost, streamName)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"rtmp_url": rtmpURL, "stream_name": streamName,
		"playback_url": playbackURL,
		"source_type":  sourceType, "source_id": sourceID, "display_name": displayName,
	})
}
