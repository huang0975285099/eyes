package web

import (
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"media-service/analysis"
	"media-service/database"
	"media-service/models"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

const maxAIEventBatch = 100

var eventIDPattern = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,64}$`)

type realtimeRulePayload struct {
	AlgorithmCode string         `json:"algorithm_code"`
	Config        map[string]any `json:"config"`
}

type realtimeStreamPayload struct {
	VideoSourceID uint                  `json:"video_source_id"`
	CustomerID    uint                  `json:"customer_id"`
	StreamName    string                `json:"stream_name"`
	InputURL      string                `json:"input_url"`
	FallbackURL   string                `json:"fallback_url"`
	Rules         []realtimeRulePayload `json:"rules"`
}

// handleAIRealtimeConfig is the control-plane snapshot consumed by the
// long-running AIService stream manager. Only active, enabled sources and
// enabled realtime rules are returned.
func (s *Server) handleAIRealtimeConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	workerID := strings.TrimSpace(r.URL.Query().Get("worker_id"))
	if workerID == "" || len(workerID) > 100 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "worker_id不能为空且最多100个字符"})
		return
	}
	capabilities := cleanCapabilities(strings.Split(r.URL.Query().Get("capabilities"), ","))
	if len(capabilities) == 0 {
		writeJSON(w, http.StatusOK, map[string]any{"generated_at": time.Now(), "streams": []realtimeStreamPayload{}})
		return
	}
	active := make(map[string]struct{})
	for _, stream := range s.fetchSRSStreams() {
		active[stream.Name] = struct{}{}
	}
	if len(active) == 0 {
		writeJSON(w, http.StatusOK, map[string]any{"generated_at": time.Now(), "streams": []realtimeStreamPayload{}})
		return
	}

	var sources []models.VideoSource
	if err := database.DB.Where("enabled = ? AND stream_name IN ?", true, mapKeys(active)).Find(&sources).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询实时AI视频源失败"})
		return
	}
	if len(sources) == 0 {
		writeJSON(w, http.StatusOK, map[string]any{"generated_at": time.Now(), "streams": []realtimeStreamPayload{}})
		return
	}

	sourceIDs := make([]uint, 0, len(sources))
	for _, source := range sources {
		sourceIDs = append(sourceIDs, source.ID)
	}
	var rules []models.VideoAnalysisRule
	if err := database.DB.Where(
		"video_source_id IN ? AND enabled = ? AND algorithm_code <> ? AND algorithm_code IN ? AND algorithm_code IN (?)",
		sourceIDs, true, analysis.AlgorithmFrameSampler, capabilities,
		database.DB.Model(&models.AIAlgorithm{}).Select("code").Where("enabled = ?", true),
	).Order("algorithm_code").Find(&rules).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询实时AI规则失败"})
		return
	}
	rulesBySource := make(map[uint][]realtimeRulePayload)
	for _, rule := range rules {
		rulesBySource[rule.VideoSourceID] = append(rulesBySource[rule.VideoSourceID], realtimeRulePayload{
			AlgorithmCode: rule.AlgorithmCode,
			Config:        decodeAlgorithmConfig(rule.ConfigJSON),
		})
	}
	payloads := make([]realtimeStreamPayload, 0, len(sources))
	for _, source := range sources {
		streamRules := rulesBySource[source.ID]
		if len(streamRules) == 0 {
			continue
		}
		payloads = append(payloads, realtimeStreamPayload{
			VideoSourceID: source.ID, CustomerID: source.CustomerID,
			StreamName:  source.StreamName,
			InputURL:    buildInternalRTMPURL(s.InternalRTMPHost, source.StreamName),
			FallbackURL: buildInternalHLSURL(s.AIStreamBaseURL, source.StreamName),
			Rules:       streamRules,
		})
	}
	payloads = s.assignRealtimeStreams(workerID, capabilities, payloads)
	writeJSON(w, http.StatusOK, map[string]any{"generated_at": time.Now(), "streams": payloads})
}

func (s *Server) assignRealtimeStreams(workerID string, capabilities []string, streams []realtimeStreamPayload) []realtimeStreamPayload {
	workers := map[string]map[string]struct{}{workerID: capabilitySet(capabilities)}
	var peers []models.AIWorker
	if err := database.DB.Where("heartbeat_at >= ? AND status IN ?", time.Now().Add(-30*time.Second), []string{"online", "idle", "running"}).Find(&peers).Error; err == nil {
		for _, peer := range peers {
			if peer.WorkerID == workerID {
				// The request is newer than the last persisted heartbeat and is
				// authoritative during rolling upgrades.
				continue
			}
			var values []string
			if json.Unmarshal([]byte(peer.CapabilitiesJSON), &values) == nil {
				workers[peer.WorkerID] = capabilitySet(cleanCapabilities(values))
			}
		}
	}
	assigned := make([]realtimeStreamPayload, 0, len(streams))
	for _, stream := range streams {
		required := make([]string, 0, len(stream.Rules))
		for _, rule := range stream.Rules {
			required = append(required, rule.AlgorithmCode)
		}
		if chooseRealtimeWorker(stream.StreamName, required, workers) == workerID {
			assigned = append(assigned, stream)
		}
	}
	return assigned
}

func capabilitySet(values []string) map[string]struct{} {
	result := make(map[string]struct{}, len(values))
	for _, value := range values {
		result[value] = struct{}{}
	}
	return result
}

func chooseRealtimeWorker(streamName string, required []string, workers map[string]map[string]struct{}) string {
	eligible := make([]string, 0, len(workers))
	for workerID, capabilities := range workers {
		supportsAll := true
		for _, code := range required {
			if _, ok := capabilities[code]; !ok {
				supportsAll = false
				break
			}
		}
		if supportsAll {
			eligible = append(eligible, workerID)
		}
	}
	if len(eligible) == 0 {
		return ""
	}
	sort.Strings(eligible)
	hash := fnv.New32a()
	_, _ = hash.Write([]byte(streamName))
	return eligible[int(hash.Sum32())%len(eligible)]
}

type aiEventArtifact struct {
	EventID       string         `json:"event_id"`
	VideoSourceID uint           `json:"video_source_id"`
	StreamName    string         `json:"stream_name"`
	AlgorithmCode string         `json:"algorithm_code"`
	EventType     string         `json:"event_type"`
	Confidence    float64        `json:"confidence"`
	StartedAt     time.Time      `json:"started_at"`
	EndedAt       *time.Time     `json:"ended_at"`
	SnapshotPath  string         `json:"snapshot_path"`
	ClipPath      string         `json:"clip_path"`
	ModelVersion  string         `json:"model_version"`
	Metadata      map[string]any `json:"metadata"`
}

type aiEventsRequest struct {
	WorkerID string            `json:"worker_id"`
	Events   []aiEventArtifact `json:"events"`
}

// handleAIEvents stores completed realtime events idempotently. Source and
// customer identity always come from the database rather than worker input.
func (s *Server) handleAIEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req aiEventsRequest
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	req.WorkerID = strings.TrimSpace(req.WorkerID)
	if req.WorkerID == "" || len(req.WorkerID) > 100 || len(req.Events) == 0 || len(req.Events) > maxAIEventBatch {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "worker_id不能为空，events必须包含1到100项"})
		return
	}

	sourceIDs := make([]uint, 0, len(req.Events))
	for _, event := range req.Events {
		sourceIDs = append(sourceIDs, event.VideoSourceID)
	}
	var sources []models.VideoSource
	if err := database.DB.Where("id IN ?", sourceIDs).Find(&sources).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询AI事件视频源失败"})
		return
	}
	sourceByID := make(map[uint]models.VideoSource, len(sources))
	for _, source := range sources {
		sourceByID[source.ID] = source
	}
	var algorithms []models.AIAlgorithm
	if err := database.DB.Find(&algorithms).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询AI算法目录失败"})
		return
	}
	algorithmCodes := make(map[string]struct{}, len(algorithms))
	for _, algorithm := range algorithms {
		algorithmCodes[algorithm.Code] = struct{}{}
	}

	upserted := 0
	err := database.DB.Transaction(func(tx *gorm.DB) error {
		for _, artifact := range req.Events {
			artifact.EventID = strings.TrimSpace(artifact.EventID)
			artifact.AlgorithmCode = strings.ToLower(strings.TrimSpace(artifact.AlgorithmCode))
			artifact.EventType = strings.ToLower(strings.TrimSpace(artifact.EventType))
			source, ok := sourceByID[artifact.VideoSourceID]
			if !ok {
				return fmt.Errorf("视频源%d不存在", artifact.VideoSourceID)
			}
			if err := s.validateAIEventArtifact(artifact, source, algorithmCodes); err != nil {
				return err
			}
			metadata, err := json.Marshal(artifact.Metadata)
			if err != nil || len(metadata) > 64*1024 {
				return errors.New("事件metadata无效或超过64KB")
			}
			event := models.AIEvent{
				CustomerID: source.CustomerID, EventID: artifact.EventID,
				VideoSourceID: source.ID, StreamName: source.StreamName,
				AlgorithmCode: artifact.AlgorithmCode, EventType: artifact.EventType,
				Confidence: artifact.Confidence, StartedAt: artifact.StartedAt,
				EndedAt: artifact.EndedAt, SnapshotPath: artifact.SnapshotPath,
				ClipPath: artifact.ClipPath, ModelVersion: artifact.ModelVersion,
				Status: "pending", MetadataJSON: string(metadata),
			}
			onConflict := clause.OnConflict{
				Columns:   []clause.Column{{Name: "event_id"}},
				DoNothing: true,
			}
			phase, _ := artifact.Metadata["event_phase"].(string)
			if strings.EqualFold(phase, "closed") || artifact.EndedAt != nil {
				onConflict = clause.OnConflict{
					Columns: []clause.Column{{Name: "event_id"}},
					DoUpdates: clause.AssignmentColumns([]string{
						"confidence", "ended_at", "snapshot_path", "clip_path",
						"model_version", "metadata_json", "updated_at",
					}),
				}
			}
			result := tx.Clauses(onConflict).Create(&event)
			if result.Error != nil {
				return result.Error
			}
			upserted += int(result.RowsAffected)
		}
		return nil
	})
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "upserted": upserted})
}

func (s *Server) validateAIEventArtifact(artifact aiEventArtifact, source models.VideoSource, algorithms map[string]struct{}) error {
	if !eventIDPattern.MatchString(artifact.EventID) {
		return errors.New("event_id格式无效")
	}
	if artifact.StreamName != "" && artifact.StreamName != source.StreamName {
		return errors.New("事件stream_name与视频源不匹配")
	}
	if _, ok := algorithms[artifact.AlgorithmCode]; !ok {
		return fmt.Errorf("未知AI算法%s", artifact.AlgorithmCode)
	}
	if artifact.EventType == "" || len(artifact.EventType) > 50 || artifact.Confidence < 0 || artifact.Confidence > 1 {
		return errors.New("事件类型或置信度无效")
	}
	if artifact.StartedAt.IsZero() || (artifact.EndedAt != nil && artifact.EndedAt.Before(artifact.StartedAt)) {
		return errors.New("事件时间无效")
	}
	if len(artifact.ModelVersion) > 100 {
		return errors.New("model_version最多100个字符")
	}
	root := filepath.Join(s.RecordingOutputDir, "_events")
	if err := validateEventFile(root, artifact.SnapshotPath, []string{".jpg", ".jpeg"}, true); err != nil {
		return fmt.Errorf("事件截图无效: %w", err)
	}
	if err := validateEventFile(root, artifact.ClipPath, []string{".mp4"}, false); err != nil {
		return fmt.Errorf("事件短视频无效: %w", err)
	}
	return nil
}

func validateEventFile(root, path string, extensions []string, required bool) error {
	path = strings.TrimSpace(path)
	if path == "" {
		if required {
			return errors.New("文件路径不能为空")
		}
		return nil
	}
	if !pathIsWithin(root, path) {
		return errors.New("文件必须位于共享_events目录中")
	}
	ext := strings.ToLower(filepath.Ext(path))
	validExtension := false
	for _, candidate := range extensions {
		if ext == candidate {
			validExtension = true
			break
		}
	}
	if !validExtension {
		return errors.New("文件类型不受支持")
	}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 {
		return errors.New("文件不存在或为空")
	}
	return nil
}

type portalEventRow struct {
	models.AIEvent
	DisplayName string         `json:"display_name"`
	Metadata    map[string]any `json:"metadata"`
}

func (s *Server) handlePortalEvents(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	query := database.DB.Model(&models.AIEvent{})
	if !p.isPlatformAdmin() {
		query = query.Where("customer_id = ?", p.User.CustomerID)
	}
	if value := strings.TrimSpace(r.URL.Query().Get("stream_name")); value != "" {
		query = query.Where("stream_name = ?", value)
	}
	if value := strings.TrimSpace(r.URL.Query().Get("algorithm_code")); value != "" {
		query = query.Where("algorithm_code = ?", value)
	}
	if value := strings.TrimSpace(r.URL.Query().Get("status")); value != "" {
		query = query.Where("status = ?", value)
	}
	if value := r.URL.Query().Get("date"); value != "" {
		if date, err := time.ParseInLocation("2006-01-02", value, time.Local); err == nil {
			query = query.Where("started_at >= ? AND started_at < ?", date, date.AddDate(0, 0, 1))
		}
	}
	var events []models.AIEvent
	if err := query.Order("started_at DESC").Limit(500).Find(&events).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询AI事件失败"})
		return
	}
	sourceIDs := make([]uint, 0, len(events))
	for _, event := range events {
		sourceIDs = append(sourceIDs, event.VideoSourceID)
	}
	var sources []models.VideoSource
	if len(sourceIDs) > 0 {
		database.DB.Where("id IN ?", sourceIDs).Find(&sources)
	}
	sourceNames := make(map[uint]string, len(sources))
	for _, source := range sources {
		sourceNames[source.ID] = source.DisplayName
	}
	rows := make([]portalEventRow, 0, len(events))
	for _, event := range events {
		metadata := map[string]any{}
		_ = json.Unmarshal([]byte(event.MetadataJSON), &metadata)
		rows = append(rows, portalEventRow{AIEvent: event, DisplayName: sourceNames[event.VideoSourceID], Metadata: metadata})
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": rows})
}

func (s *Server) handlePortalEventArtifact(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/portal/events/"), "/")
	parts := strings.Split(path, "/")
	if len(parts) != 2 {
		http.NotFound(w, r)
		return
	}
	id, err := strconv.ParseUint(parts[0], 10, 64)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	query := database.DB.Model(&models.AIEvent{}).Where("id = ?", id)
	if !p.isPlatformAdmin() {
		query = query.Where("customer_id = ?", p.User.CustomerID)
	}
	var event models.AIEvent
	if err := query.First(&event).Error; err != nil {
		http.NotFound(w, r)
		return
	}
	switch parts[1] {
	case "snapshot":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		s.serveEventFile(w, r, event.SnapshotPath, "image/jpeg")
	case "clip":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		s.serveEventFile(w, r, event.ClipPath, "video/mp4")
	case "review":
		if r.Method != http.MethodPut {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			Status string `json:"status"`
		}
		if err := decodeJSONBody(r, &req); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		status := strings.ToLower(strings.TrimSpace(req.Status))
		if status != "pending" && status != "confirmed" && status != "rejected" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "status必须为pending、confirmed或rejected"})
			return
		}
		if err := database.DB.Model(&event).Update("status", status).Error; err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "保存复核结果失败"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": status})
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) serveEventFile(w http.ResponseWriter, r *http.Request, path, contentType string) {
	if strings.TrimSpace(path) == "" || !pathIsWithin(filepath.Join(s.RecordingOutputDir, "_events"), path) {
		http.NotFound(w, r)
		return
	}
	file, err := os.Open(path)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Disposition", "inline; filename=\""+filepath.Base(path)+"\"")
	http.ServeContent(w, r, filepath.Base(path), info.ModTime(), file)
}

func mapKeys(values map[string]struct{}) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	return keys
}
