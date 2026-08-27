package web

import (
	"encoding/json"
	"media-service/analysis"
	"media-service/database"
	"media-service/models"
	"net/http"
	"strings"

	"gorm.io/gorm/clause"
)

type portalAnalysisRule struct {
	VideoSourceID uint           `json:"video_source_id"`
	AlgorithmCode string         `json:"algorithm_code"`
	Enabled       bool           `json:"enabled"`
	Config        map[string]any `json:"config"`
}

// handlePortalAnalysisRules is the generic per-source control plane used by
// all realtime algorithms after frame_sampler. Algorithm-specific UIs can
// store their parameters without adding another table or endpoint.
func (s *Server) handlePortalAnalysisRules(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	switch r.Method {
	case http.MethodGet:
		s.listPortalAnalysisRules(w, r, p)
	case http.MethodPut:
		s.updatePortalAnalysisRule(w, r, p)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) listPortalAnalysisRules(w http.ResponseWriter, r *http.Request, p principal) {
	query := database.DB.Model(&models.VideoAnalysisRule{}).
		Where("algorithm_code <> ?", analysis.AlgorithmFrameSampler)
	if !p.isPlatformAdmin() {
		query = query.Where("video_source_id IN (?)", database.DB.Model(&models.VideoSource{}).
			Select("id").Where("customer_id = ?", p.User.CustomerID))
	}
	if value := strings.TrimSpace(r.URL.Query().Get("algorithm_code")); value != "" {
		query = query.Where("algorithm_code = ?", strings.ToLower(value))
	}
	var rules []models.VideoAnalysisRule
	if err := query.Order("video_source_id, algorithm_code").Find(&rules).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询AI分析规则失败"})
		return
	}
	rows := make([]portalAnalysisRule, 0, len(rules))
	for _, rule := range rules {
		rows = append(rows, portalAnalysisRule{
			VideoSourceID: rule.VideoSourceID, AlgorithmCode: rule.AlgorithmCode,
			Enabled: rule.Enabled, Config: decodeAlgorithmConfig(rule.ConfigJSON),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"rules": rows})
}

func (s *Server) updatePortalAnalysisRule(w http.ResponseWriter, r *http.Request, p principal) {
	var req portalAnalysisRule
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	req.AlgorithmCode = strings.ToLower(strings.TrimSpace(req.AlgorithmCode))
	if req.VideoSourceID == 0 || req.AlgorithmCode == "" || req.AlgorithmCode == analysis.AlgorithmFrameSampler {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "AI分析规则参数无效"})
		return
	}
	if req.Config == nil {
		req.Config = map[string]any{}
	}
	configJSON, err := json.Marshal(req.Config)
	if err != nil || len(configJSON) > 64*1024 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "算法配置无效或超过64KB"})
		return
	}
	sourceQuery := database.DB.Model(&models.VideoSource{}).Where("id = ? AND enabled = ?", req.VideoSourceID, true)
	if !p.isPlatformAdmin() {
		sourceQuery = sourceQuery.Where("customer_id = ?", p.User.CustomerID)
	}
	var source models.VideoSource
	if err := sourceQuery.First(&source).Error; err != nil {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "无权配置该视频源"})
		return
	}
	var algorithm models.AIAlgorithm
	if err := database.DB.Where("code = ? AND enabled = ?", req.AlgorithmCode, true).First(&algorithm).Error; err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "算法不存在或尚未启用"})
		return
	}
	rule := models.VideoAnalysisRule{
		VideoSourceID: req.VideoSourceID, AlgorithmCode: req.AlgorithmCode,
		Enabled: req.Enabled, ConfigJSON: string(configJSON),
	}
	if err := database.DB.Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "video_source_id"}, {Name: "algorithm_code"}},
		DoUpdates: clause.AssignmentColumns([]string{"enabled", "config_json", "updated_at"}),
	}).Create(&rule).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "保存AI分析规则失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "rule": req})
}
