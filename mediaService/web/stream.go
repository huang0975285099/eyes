package web

import (
	"encoding/json"
	"fmt"
	"media-service/database"
	"media-service/models"
	"media-service/streamsource"
	"net"
	"net/http"
	"strings"

	"gorm.io/gorm/clause"
)

type publishConfigRequest struct {
	MAC               string `json:"mac"`
	SourceType        string `json:"source_type"`
	SourceID          string `json:"source_id"`
	DisplayName       string `json:"display_name"`
	OperatorName      string `json:"operator_name"`
	OperatorNameForce bool   `json:"operator_name_force"`
	Hostname          string `json:"hostname"`
	LocalIP           string `json:"local_ip"`
}

func normalizePublishMetadata(input publishConfigRequest) (string, string, string, error) {
	operatorName := strings.TrimSpace(input.OperatorName)
	hostname := strings.TrimSpace(input.Hostname)
	localIP := strings.TrimSpace(input.LocalIP)
	if len([]rune(operatorName)) > 20 {
		return "", "", "", fmt.Errorf("点位负责人不能超过20个字符")
	}
	if len([]rune(hostname)) > 200 {
		return "", "", "", fmt.Errorf("主机名不能超过200个字符")
	}
	if localIP != "" && net.ParseIP(localIP) == nil {
		return "", "", "", fmt.Errorf("内网IP格式无效")
	}
	return operatorName, hostname, localIP, nil
}

func privateRemoteIP(remoteAddress string) string {
	host, _, err := net.SplitHostPort(strings.TrimSpace(remoteAddress))
	if err != nil {
		host = strings.Trim(strings.TrimSpace(remoteAddress), "[]")
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsPrivate() {
		return ""
	}
	return ip.String()
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
	operatorName, hostname, localIP, err := normalizePublishMetadata(input)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "本机信息无效: " + err.Error()})
		return
	}
	if localIP == "" {
		localIP = privateRemoteIP(r.RemoteAddr)
	}
	streamName := streamsource.Name(mac, sourceType, sourceID)
	source := models.VideoSource{
		MAC: mac, SourceType: sourceType, SourceID: sourceID,
		DisplayName: displayName, OperatorName: operatorName, Hostname: hostname, LocalIP: localIP,
		PublishMode: "app", Enabled: true, StreamName: streamName,
	}
	if err := database.DB.Clauses(clause.OnConflict{
		Columns: []clause.Column{{Name: "stream_name"}},
		DoUpdates: clause.AssignmentColumns([]string{
			"mac", "source_type", "source_id", "display_name", "hostname", "local_ip",
			"publish_mode", "enabled", "updated_at",
		}),
	}).Create(&source).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "登记视频源失败"})
		return
	}
	operatorUpdate := database.DB.Model(&models.VideoSource{}).Where("stream_name = ?", streamName)
	if !input.OperatorNameForce {
		operatorUpdate = operatorUpdate.Where("operator_name = '' OR operator_name IS NULL")
	}
	if operatorName != "" {
		if err := operatorUpdate.Update("operator_name", operatorName).Error; err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "保存点位负责人失败"})
			return
		}
	}
	if err := database.DB.Where("stream_name = ?", streamName).First(&source).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "读取视频源信息失败"})
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
		"operator_name": source.OperatorName, "hostname": source.Hostname, "local_ip": source.LocalIP,
	})
}
