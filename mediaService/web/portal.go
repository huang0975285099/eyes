package web

import (
	"encoding/json"
	"errors"
	"media-service/analysis"
	"media-service/database"
	"media-service/models"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

type portalSourceRow struct {
	VideoSourceID       uint       `json:"video_source_id"`
	CustomerID          uint       `json:"customer_id"`
	CustomerName        string     `json:"customer_name,omitempty"`
	StreamName          string     `json:"stream_name"`
	DisplayName         string     `json:"display_name"`
	SourceType          string     `json:"source_type"`
	SourceID            string     `json:"source_id"`
	MAC                 string     `json:"mac"`
	Brand               string     `json:"brand"`
	PublishMode         string     `json:"publish_mode"`
	Enabled             bool       `json:"enabled"`
	Active              bool       `json:"active"`
	Codec               string     `json:"codec"`
	Width               int        `json:"width"`
	Height              int        `json:"height"`
	RecordingEnabled    bool       `json:"recording_enabled"`
	RecordingRetainDays int        `json:"recording_retain_days"`
	SamplingEnabled     bool       `json:"sampling_enabled"`
	FramesPerMinute     int        `json:"frames_per_minute"`
	FrameCount          int64      `json:"frame_count"`
	LastCapturedAt      *time.Time `json:"last_captured_at,omitempty"`
}

type portalSourceConfig struct {
	VideoSourceID       uint `json:"video_source_id"`
	RecordingEnabled    bool `json:"recording_enabled"`
	RecordingRetainDays int  `json:"recording_retain_days"`
	SamplingEnabled     bool `json:"sampling_enabled"`
	FramesPerMinute     int  `json:"frames_per_minute"`
}

func (s *Server) handlePortalSources(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	switch r.Method {
	case http.MethodGet:
		s.listPortalSources(w, p)
	case http.MethodPut:
		s.updatePortalSourceConfigs(w, r, p)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) listPortalSources(w http.ResponseWriter, p principal) {
	query := database.DB.Model(&models.VideoSource{})
	if !p.isPlatformAdmin() {
		query = query.Where("customer_id = ?", p.User.CustomerID)
	}
	var sources []models.VideoSource
	if err := query.Order("display_name, id").Find(&sources).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询视频源失败"})
		return
	}

	sourceIDs := make([]uint, 0, len(sources))
	streamNames := make([]string, 0, len(sources))
	for _, source := range sources {
		sourceIDs = append(sourceIDs, source.ID)
		streamNames = append(streamNames, source.StreamName)
	}
	var recordingRules []models.VideoRecordingRule
	var analysisRules []models.VideoAnalysisRule
	if len(sourceIDs) > 0 {
		database.DB.Where("video_source_id IN ?", sourceIDs).Find(&recordingRules)
		database.DB.Where("video_source_id IN ? AND algorithm_code = ?", sourceIDs, analysis.AlgorithmFrameSampler).Find(&analysisRules)
	}
	recordingBySource := make(map[uint]models.VideoRecordingRule, len(recordingRules))
	for _, rule := range recordingRules {
		recordingBySource[rule.VideoSourceID] = rule
	}
	analysisBySource := make(map[uint]models.VideoAnalysisRule, len(analysisRules))
	for _, rule := range analysisRules {
		analysisBySource[rule.VideoSourceID] = rule
	}

	type aggregate struct {
		StreamName     string     `gorm:"column:stream_name"`
		FrameCount     int64      `gorm:"column:frame_count"`
		LastCapturedAt *time.Time `gorm:"column:last_captured_at"`
	}
	var aggregates []aggregate
	if len(streamNames) > 0 {
		frameQuery := database.DB.Model(&models.RecordingFrame{}).
			Select("stream_name, COUNT(*) AS frame_count, MAX(captured_at) AS last_captured_at").
			Where("segment_id = 0 AND stream_name IN ?", streamNames)
		if !p.isPlatformAdmin() {
			frameQuery = frameQuery.Where("customer_id = ?", p.User.CustomerID)
		}
		_ = frameQuery.Group("stream_name").Scan(&aggregates).Error
	}
	aggregateByStream := make(map[string]aggregate, len(aggregates))
	for _, item := range aggregates {
		aggregateByStream[item.StreamName] = item
	}

	active := make(map[string]srsStream)
	for _, stream := range s.fetchSRSStreams() {
		active[stream.Name] = stream
	}
	customerNames := map[uint]string{}
	if p.isPlatformAdmin() {
		var customers []models.Customer
		database.DB.Find(&customers)
		for _, customer := range customers {
			customerNames[customer.ID] = customer.Name
		}
	}

	rows := make([]portalSourceRow, 0, len(sources))
	for _, source := range sources {
		stream, isActive := active[source.StreamName]
		recordingRule := recordingBySource[source.ID]
		retainDays := recordingRule.RetainDays
		if retainDays <= 0 {
			retainDays = s.DefaultRetainDays
		}
		analysisRule := analysisBySource[source.ID]
		framesPerMinute := 2
		if strings.TrimSpace(analysisRule.ConfigJSON) != "" {
			var config struct {
				FramesPerMinute int `json:"frames_per_minute"`
			}
			if json.Unmarshal([]byte(analysisRule.ConfigJSON), &config) == nil && config.FramesPerMinute > 0 {
				framesPerMinute = config.FramesPerMinute
			}
		}
		aggregate := aggregateByStream[source.StreamName]
		rows = append(rows, portalSourceRow{
			VideoSourceID: source.ID, CustomerID: source.CustomerID, CustomerName: customerNames[source.CustomerID],
			StreamName: source.StreamName, DisplayName: source.DisplayName, SourceType: source.SourceType,
			SourceID: source.SourceID, MAC: source.MAC, Brand: source.Brand,
			PublishMode: source.PublishMode, Enabled: source.Enabled, Active: isActive,
			Codec: stream.Video.Codec, Width: stream.Video.Width, Height: stream.Video.Height,
			RecordingEnabled: recordingRule.Enabled, RecordingRetainDays: retainDays,
			SamplingEnabled: analysisRule.Enabled, FramesPerMinute: framesPerMinute,
			FrameCount: aggregate.FrameCount, LastCapturedAt: aggregate.LastCapturedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"sources": rows})
}

func (s *Server) updatePortalSourceConfigs(w http.ResponseWriter, r *http.Request, p principal) {
	var req struct {
		Sources []portalSourceConfig `json:"sources"`
	}
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if len(req.Sources) == 0 || len(req.Sources) > 1000 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sources不能为空且最多1000项"})
		return
	}
	ids := make([]uint, 0, len(req.Sources))
	seen := make(map[uint]struct{}, len(req.Sources))
	for _, config := range req.Sources {
		if config.VideoSourceID == 0 || config.RecordingRetainDays < 1 || config.RecordingRetainDays > 3650 ||
			config.FramesPerMinute < 1 || config.FramesPerMinute > 60 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "视频源配置参数无效"})
			return
		}
		if _, duplicate := seen[config.VideoSourceID]; duplicate {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "视频源不能重复"})
			return
		}
		seen[config.VideoSourceID] = struct{}{}
		ids = append(ids, config.VideoSourceID)
	}
	sourceQuery := database.DB.Model(&models.VideoSource{}).Where("id IN ?", ids)
	if !p.isPlatformAdmin() {
		sourceQuery = sourceQuery.Where("customer_id = ?", p.User.CustomerID)
	}
	var accessible int64
	if err := sourceQuery.Count(&accessible).Error; err != nil || accessible != int64(len(ids)) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "包含无权管理的视频源"})
		return
	}

	err := database.DB.Transaction(func(tx *gorm.DB) error {
		for _, config := range req.Sources {
			recordingRule := models.VideoRecordingRule{
				VideoSourceID: config.VideoSourceID, Enabled: config.RecordingEnabled,
				RetainDays: config.RecordingRetainDays,
			}
			if err := tx.Clauses(clause.OnConflict{
				Columns:   []clause.Column{{Name: "video_source_id"}},
				DoUpdates: clause.AssignmentColumns([]string{"enabled", "retain_days", "updated_at"}),
			}).Create(&recordingRule).Error; err != nil {
				return err
			}
			configJSON, _ := json.Marshal(map[string]int{"frames_per_minute": config.FramesPerMinute})
			analysisRule := models.VideoAnalysisRule{
				VideoSourceID: config.VideoSourceID, AlgorithmCode: analysis.AlgorithmFrameSampler,
				Enabled: config.SamplingEnabled, ConfigJSON: string(configJSON),
			}
			if err := tx.Clauses(clause.OnConflict{
				Columns:   []clause.Column{{Name: "video_source_id"}, {Name: "algorithm_code"}},
				DoUpdates: clause.AssignmentColumns([]string{"enabled", "config_json", "updated_at"}),
			}).Create(&analysisRule).Error; err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "保存视频源服务配置失败"})
		return
	}
	_ = analysis.ResetPendingLiveFrameSamplerJobs()
	if s.RefreshRecordingRules != nil {
		s.RefreshRecordingRules()
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "updated": len(req.Sources)})
}

func (s *Server) handlePortalCustomers(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	if !p.isPlatformAdmin() {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "仅平台管理员可以管理客户"})
		return
	}
	switch r.Method {
	case http.MethodGet:
		var customers []models.Customer
		if err := database.DB.Order("name, id").Find(&customers).Error; err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询客户失败"})
			return
		}
		var users []models.User
		if err := database.DB.Where("role = ?", roleCustomerAdmin).Order("id").Find(&users).Error; err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询客户账号失败"})
			return
		}
		usernames := make(map[uint]string, len(users))
		for _, user := range users {
			if _, exists := usernames[user.CustomerID]; !exists {
				usernames[user.CustomerID] = user.Username
			}
		}
		type customerRow struct {
			ID       uint   `json:"id"`
			Name     string `json:"name"`
			Username string `json:"username"`
			Enabled  bool   `json:"enabled"`
		}
		rows := make([]customerRow, 0, len(customers))
		for _, customer := range customers {
			rows = append(rows, customerRow{ID: customer.ID, Name: customer.Name, Username: usernames[customer.ID], Enabled: customer.Enabled})
		}
		writeJSON(w, http.StatusOK, map[string]any{"customers": rows})
	case http.MethodPost:
		s.createPortalCustomer(w, r)
	case http.MethodPut:
		s.updatePortalCustomer(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) updatePortalCustomer(w http.ResponseWriter, r *http.Request) {
	var req struct {
		CustomerID  uint   `json:"customer_id"`
		Enabled     *bool  `json:"enabled"`
		NewPassword string `json:"new_password"`
	}
	if err := decodeJSONBody(r, &req); err != nil || req.CustomerID == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "客户账号参数无效"})
		return
	}
	if req.Enabled == nil && strings.TrimSpace(req.NewPassword) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "没有需要修改的内容"})
		return
	}
	var user models.User
	if err := database.DB.Where("customer_id = ? AND role = ?", req.CustomerID, roleCustomerAdmin).Order("id").First(&user).Error; err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "客户账号不存在"})
		return
	}
	passwordHash := ""
	if strings.TrimSpace(req.NewPassword) != "" {
		var err error
		_, passwordHash, err = validatedCredentials(user.Username, req.NewPassword)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
	}
	err := database.DB.Transaction(func(tx *gorm.DB) error {
		if req.Enabled != nil {
			if err := tx.Model(&models.Customer{}).Where("id = ?", req.CustomerID).Update("enabled", *req.Enabled).Error; err != nil {
				return err
			}
			if err := tx.Model(&models.User{}).Where("customer_id = ?", req.CustomerID).Update("enabled", *req.Enabled).Error; err != nil {
				return err
			}
		}
		if passwordHash != "" {
			if err := tx.Model(&models.User{}).Where("id = ?", user.ID).Update("password_hash", passwordHash).Error; err != nil {
				return err
			}
		}
		if passwordHash != "" || (req.Enabled != nil && !*req.Enabled) {
			return tx.Where("user_id IN (?)", tx.Model(&models.User{}).Select("id").Where("customer_id = ?", req.CustomerID)).Delete(&models.UserSession{}).Error
		}
		return nil
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "更新客户账号失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) createPortalCustomer(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name     string `json:"name"`
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	name := strings.TrimSpace(req.Name)
	if len([]rune(name)) < 2 || len([]rune(name)) > 100 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "客户名称必须为2到100个字符"})
		return
	}
	username, passwordHash, err := validatedCredentials(req.Username, req.Password)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	var customer models.Customer
	err = database.DB.Transaction(func(tx *gorm.DB) error {
		customer = models.Customer{Name: name, Enabled: true}
		if err := tx.Create(&customer).Error; err != nil {
			return err
		}
		return tx.Create(&models.User{
			CustomerID: customer.ID, Username: username, PasswordHash: passwordHash,
			Role: roleCustomerAdmin, Enabled: true,
		}).Error
	})
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "客户名称或登录账号已经存在"})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": customer.ID, "name": customer.Name, "username": username, "enabled": customer.Enabled,
	})
}

func (s *Server) handlePortalSourceOwner(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	if !p.isPlatformAdmin() || r.Method != http.MethodPut {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "仅平台管理员可以分配视频源"})
		return
	}
	var req struct {
		VideoSourceID uint `json:"video_source_id"`
		CustomerID    uint `json:"customer_id"`
	}
	if err := decodeJSONBody(r, &req); err != nil || req.VideoSourceID == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "分配参数无效"})
		return
	}
	if req.CustomerID != 0 {
		var count int64
		if err := database.DB.Model(&models.Customer{}).Where("id = ? AND enabled = ?", req.CustomerID, true).Count(&count).Error; err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询客户失败"})
			return
		}
		if count != 1 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "客户不存在或已停用"})
			return
		}
	}
	err := database.DB.Transaction(func(tx *gorm.DB) error {
		result := tx.Model(&models.VideoSource{}).Where("id = ?", req.VideoSourceID).Update("customer_id", req.CustomerID)
		if result.Error != nil {
			return result.Error
		}
		if result.RowsAffected != 1 {
			return gorm.ErrRecordNotFound
		}
		// A new owner must explicitly choose services; never inherit the previous
		// customer's recording or AI switches.
		if err := tx.Model(&models.VideoRecordingRule{}).Where("video_source_id = ?", req.VideoSourceID).Update("enabled", false).Error; err != nil {
			return err
		}
		return tx.Model(&models.VideoAnalysisRule{}).Where("video_source_id = ?", req.VideoSourceID).Update("enabled", false).Error
	})
	if errors.Is(err, gorm.ErrRecordNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "视频源不存在"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "分配视频源失败"})
		return
	}
	_ = analysis.ResetPendingLiveFrameSamplerJobs()
	if s.RefreshRecordingRules != nil {
		s.RefreshRecordingRules()
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) handlePortalFrames(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	query := database.DB.Model(&models.RecordingFrame{}).Where("segment_id = 0")
	if !p.isPlatformAdmin() {
		query = query.Where("customer_id = ?", p.User.CustomerID)
	}
	if streamName := strings.TrimSpace(r.URL.Query().Get("stream_name")); streamName != "" {
		query = query.Where("stream_name = ?", streamName)
	}
	if dateValue := r.URL.Query().Get("date"); dateValue != "" {
		if date, err := time.ParseInLocation("2006-01-02", dateValue, time.Local); err == nil {
			query = query.Where("captured_at >= ? AND captured_at < ?", date, date.AddDate(0, 0, 1))
		}
	}
	var frames []models.RecordingFrame
	if err := query.Order("captured_at DESC").Limit(500).Find(&frames).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询抽帧结果失败"})
		return
	}
	sourceNames := make(map[string]string)
	var sources []models.VideoSource
	database.DB.Where("stream_name IN ?", frameStreamNames(frames)).Find(&sources)
	for _, source := range sources {
		sourceNames[source.StreamName] = source.DisplayName
	}
	rows := make([]frameRow, 0, len(frames))
	for _, frame := range frames {
		rows = append(rows, frameRow{
			ID: frame.ID, StreamName: frame.StreamName, MAC: frame.MAC, SourceType: frame.SourceType,
			SourceID: frame.SourceID, DisplayName: sourceNames[frame.StreamName], CapturedAt: frame.CapturedAt,
			FrameIndex: frame.FrameIndex, FileSize: frame.FileSize,
		})
	}
	writeJSON(w, http.StatusOK, rows)
}

func (s *Server) handlePortalFrameImage(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	idValue := strings.TrimPrefix(r.URL.Path, "/api/portal/frames/")
	idValue = strings.TrimSuffix(idValue, "/image")
	id, err := strconv.ParseUint(strings.Trim(idValue, "/"), 10, 64)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	query := database.DB.Model(&models.RecordingFrame{}).Where("id = ? AND segment_id = 0", id)
	if !p.isPlatformAdmin() {
		query = query.Where("customer_id = ?", p.User.CustomerID)
	}
	var frame models.RecordingFrame
	if err := query.First(&frame).Error; err != nil {
		http.NotFound(w, r)
		return
	}
	file, err := os.Open(frame.FilePath)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	http.ServeContent(w, r, filepath.Base(frame.FilePath), info.ModTime(), file)
}

func (s *Server) handlePortalJobs(w http.ResponseWriter, r *http.Request) {
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	type countRow struct {
		Status string `json:"status"`
		Count  int64  `json:"count"`
	}
	query := database.DB.Model(&models.AIJob{}).
		Select("status, COUNT(*) AS count").
		Where("algorithm_code = ? AND input_type = ?", analysis.AlgorithmFrameSampler, analysis.JobInputLiveStream)
	if !p.isPlatformAdmin() {
		query = query.Where("input_ref_id IN (?)", database.DB.Model(&models.VideoSource{}).
			Select("id").Where("customer_id = ?", p.User.CustomerID))
	}
	var counts []countRow
	if err := query.Group("status").Scan(&counts).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询任务状态失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"jobs": counts})
}

func frameStreamNames(frames []models.RecordingFrame) []string {
	names := make([]string, 0, len(frames))
	for _, frame := range frames {
		names = append(names, frame.StreamName)
	}
	return names
}
