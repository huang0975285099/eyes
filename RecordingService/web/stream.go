package web

import (
	"crypto/hmac"
	"encoding/json"
	"fmt"
	"net/http"
	"recording-service/database"
	"recording-service/models"
	"recording-service/streamsource"
	"strings"

	"gorm.io/gorm/clause"
)

type publishConfigRequest struct {
	MAC         string `json:"mac"`
	SourceType  string `json:"source_type"`
	SourceID    string `json:"source_id"`
	DisplayName string `json:"display_name"`
}

type srsHookRequest struct {
	Action string `json:"action"`
	App    string `json:"app"`
	Stream string `json:"stream"`
	Param  string `json:"param"`
}

func (s *Server) handlePublishConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !s.authorizeClient(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "客户端密钥无效"})
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

func (s *Server) handleSRSPublish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	defer r.Body.Close()
	var hook srsHookRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&hook); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 1, "message": "invalid hook"})
		return
	}
	if hook.App != "live" || !s.allowedPublishStream(hook.Stream) {
		writeJSON(w, http.StatusOK, map[string]any{"code": 1, "message": "unregistered or invalid stream"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

// allowedPublishStream performs no token authentication. It accepts registered
// video sources and, for old desktop clients, a legacy MAC stream belonging to
// a computer already registered in RecordingService.
func (s *Server) allowedPublishStream(streamName string) bool {
	var count int64
	database.DB.Model(&models.VideoSource{}).
		Where("stream_name = ? AND enabled = ?", streamName, true).
		Count(&count)
	if count > 0 {
		return true
	}
	mac, sourceType, ok := streamsource.Parse(streamName)
	if !ok || sourceType != streamsource.TypeScreen {
		return false
	}
	database.DB.Model(&models.Computer{}).Where("mac = ?", mac).Count(&count)
	return count > 0
}

func (s *Server) handleSRSLifecycle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

func (s *Server) authorizeClient(r *http.Request) bool {
	provided := r.Header.Get("X-Client-Key")
	return s.ClientAPIKey != "" && hmac.Equal([]byte(provided), []byte(s.ClientAPIKey))
}

func normalizeMAC(value string) string {
	return strings.ToLower(strings.ReplaceAll(strings.TrimSpace(value), "-", ":"))
}
