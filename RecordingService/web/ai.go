package web

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"recording-service/analysis"
	"recording-service/database"
	"recording-service/models"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

const (
	defaultAIJobLeaseSeconds = 300
	maxAIJobLeaseSeconds     = 1800
	maxAIJobClaimBatch       = 32
)

type aiJobClaimRequest struct {
	WorkerID     string   `json:"worker_id"`
	Capabilities []string `json:"capabilities"`
	MaxJobs      int      `json:"max_jobs"`
	LeaseSeconds int      `json:"lease_seconds"`
}

type aiJobPayload struct {
	ID            uint      `json:"id"`
	AlgorithmCode string    `json:"algorithm_code"`
	InputType     string    `json:"input_type"`
	SegmentID     uint      `json:"segment_id"`
	StreamName    string    `json:"stream_name"`
	MAC           string    `json:"mac"`
	SourceType    string    `json:"source_type"`
	SourceID      string    `json:"source_id"`
	InputPath     string    `json:"input_path"`
	StartedAt     time.Time `json:"started_at"`
	Duration      float64   `json:"duration"`
	Attempt       int       `json:"attempt"`
	LeaseUntil    time.Time `json:"lease_until"`
}

type aiFrameArtifact struct {
	FrameIndex int       `json:"frame_index"`
	FilePath   string    `json:"file_path"`
	CapturedAt time.Time `json:"captured_at"`
}

type aiJobReportRequest struct {
	JobID     uint              `json:"job_id"`
	WorkerID  string            `json:"worker_id"`
	Success   bool              `json:"success"`
	Retryable bool              `json:"retryable"`
	Error     string            `json:"error"`
	Frames    []aiFrameArtifact `json:"frames"`
}

type aiWorkerHeartbeatRequest struct {
	WorkerID     string   `json:"worker_id"`
	Hostname     string   `json:"hostname"`
	Version      string   `json:"version"`
	Capabilities []string `json:"capabilities"`
	Status       string   `json:"status"`
	ActiveJobs   int      `json:"active_jobs"`
	LastError    string   `json:"last_error"`
}

func (s *Server) handleAIAlgorithms(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var algorithms []models.AIAlgorithm
	if err := database.DB.Order("code ASC").Find(&algorithms).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询AI算法失败"})
		return
	}
	writeJSON(w, http.StatusOK, algorithms)
}

func (s *Server) handleAIJobStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	type statusCount struct {
		AlgorithmCode string `json:"algorithm_code"`
		Status        string `json:"status"`
		Count         int64  `json:"count"`
	}
	var counts []statusCount
	if err := database.DB.Model(&models.AIJob{}).
		Select("algorithm_code, status, count(*) AS count").
		Group("algorithm_code, status").Scan(&counts).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询AI任务统计失败"})
		return
	}
	var workers []models.AIWorker
	database.DB.Order("heartbeat_at DESC").Find(&workers)
	writeJSON(w, http.StatusOK, map[string]any{"jobs": counts, "workers": workers})
}

func (s *Server) handleAIJobClaim(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req aiJobClaimRequest
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	req.WorkerID = strings.TrimSpace(req.WorkerID)
	if req.WorkerID == "" || len(req.WorkerID) > 100 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "worker_id不能为空且最多100个字符"})
		return
	}
	req.Capabilities = cleanCapabilities(req.Capabilities)
	if len(req.Capabilities) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "capabilities不能为空"})
		return
	}
	if req.MaxJobs <= 0 {
		req.MaxJobs = 1
	}
	if req.MaxJobs > maxAIJobClaimBatch {
		req.MaxJobs = maxAIJobClaimBatch
	}
	if req.LeaseSeconds <= 0 {
		req.LeaseSeconds = defaultAIJobLeaseSeconds
	}
	if req.LeaseSeconds > maxAIJobLeaseSeconds {
		req.LeaseSeconds = maxAIJobLeaseSeconds
	}

	now := time.Now()
	leaseUntil := now.Add(time.Duration(req.LeaseSeconds) * time.Second)
	var jobs []models.AIJob
	err := database.DB.Transaction(func(tx *gorm.DB) error {
		// Jobs abandoned by a dead worker become available again, until their
		// retry budget is exhausted.
		if err := tx.Model(&models.AIJob{}).
			Where("status = ? AND lease_until < ? AND attempts < max_attempts", analysis.JobStatusRunning, now).
			Updates(map[string]any{
				"status": analysis.JobStatusPending, "worker_id": "", "lease_until": nil,
				"available_at": now, "last_error": "任务租约超时，等待重新执行",
			}).Error; err != nil {
			return err
		}
		if err := tx.Model(&models.AIJob{}).
			Where("status = ? AND lease_until < ? AND attempts >= max_attempts", analysis.JobStatusRunning, now).
			Updates(map[string]any{
				"status": analysis.JobStatusFailed, "worker_id": "", "lease_until": nil,
				"finished_at": now, "last_error": "任务租约超时且已达到最大尝试次数",
			}).Error; err != nil {
			return err
		}

		if err := tx.Clauses(clause.Locking{Strength: "UPDATE", Options: "SKIP LOCKED"}).
			Where("algorithm_code IN ? AND status = ? AND available_at <= ? AND attempts < max_attempts",
				req.Capabilities, analysis.JobStatusPending, now).
			Order("priority DESC, id ASC").Limit(req.MaxJobs).Find(&jobs).Error; err != nil {
			return err
		}
		for i := range jobs {
			updates := map[string]any{
				"status": analysis.JobStatusRunning, "worker_id": req.WorkerID,
				"lease_until": leaseUntil, "attempts": gorm.Expr("attempts + 1"),
				"started_at": now, "last_error": "",
			}
			if err := tx.Model(&models.AIJob{}).Where("id = ?", jobs[i].ID).Updates(updates).Error; err != nil {
				return err
			}
			jobs[i].Attempts++
			jobs[i].WorkerID = req.WorkerID
			jobs[i].LeaseUntil = &leaseUntil
		}
		return nil
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "领取AI任务失败"})
		return
	}

	payloads := make([]aiJobPayload, 0, len(jobs))
	for _, job := range jobs {
		if job.InputType != analysis.JobInputSegment {
			continue
		}
		var segment models.RecordingSegment
		if err := database.DB.First(&segment, job.InputRefID).Error; err != nil {
			database.DB.Model(&models.AIJob{}).Where("id = ?", job.ID).Updates(map[string]any{
				"status": analysis.JobStatusFailed, "finished_at": time.Now(),
				"last_error": "输入录像不存在", "lease_until": nil,
			})
			continue
		}
		payloads = append(payloads, aiJobPayload{
			ID: job.ID, AlgorithmCode: job.AlgorithmCode, InputType: job.InputType,
			SegmentID: segment.ID, StreamName: segment.StreamName, MAC: segment.MAC,
			SourceType: segment.SourceType, SourceID: segment.SourceID,
			InputPath: segment.FilePath, StartedAt: segment.StartedAt, Duration: segment.Duration,
			Attempt: job.Attempts, LeaseUntil: leaseUntil,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"jobs": payloads})
}

func (s *Server) handleAIJobReport(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req aiJobReportRequest
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	req.WorkerID = strings.TrimSpace(req.WorkerID)
	if req.JobID == 0 || req.WorkerID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "job_id和worker_id不能为空"})
		return
	}
	if len(req.Error) > 4000 {
		req.Error = req.Error[:4000]
	}

	if req.Success {
		if err := s.validateFrameArtifacts(req.Frames); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
	}

	err := database.DB.Transaction(func(tx *gorm.DB) error {
		var job models.AIJob
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&job, req.JobID).Error; err != nil {
			return err
		}
		if job.Status == analysis.JobStatusSucceeded {
			return nil
		}
		if job.Status != analysis.JobStatusRunning || job.WorkerID != req.WorkerID {
			return errAIJobOwnership
		}

		now := time.Now()
		if !req.Success {
			if req.Retryable && job.Attempts < job.MaxAttempts {
				return tx.Model(&job).Updates(map[string]any{
					"status": analysis.JobStatusPending, "worker_id": "", "lease_until": nil,
					"available_at": now.Add(30 * time.Second), "last_error": req.Error,
				}).Error
			}
			return tx.Model(&job).Updates(map[string]any{
				"status": analysis.JobStatusFailed, "lease_until": nil,
				"last_error": req.Error, "finished_at": now,
			}).Error
		}

		if job.AlgorithmCode == analysis.AlgorithmFrameSampler {
			var segment models.RecordingSegment
			if err := tx.First(&segment, job.InputRefID).Error; err != nil {
				return err
			}
			for _, artifact := range req.Frames {
				fi, err := os.Stat(artifact.FilePath)
				if err != nil {
					return fmt.Errorf("抽帧文件不存在: %w", err)
				}
				frame := models.RecordingFrame{}
				result := tx.Where("segment_id = ? AND frame_index = ?", segment.ID, artifact.FrameIndex).
					Assign(models.RecordingFrame{
						StreamName: segment.StreamName, MAC: segment.MAC, SourceType: segment.SourceType,
						SourceID: segment.SourceID, SegmentID: segment.ID, FilePath: artifact.FilePath,
						FileSize: fi.Size(), CapturedAt: artifact.CapturedAt, FrameIndex: artifact.FrameIndex,
					}).FirstOrCreate(&frame)
				if result.Error != nil {
					return result.Error
				}
			}
		}

		return tx.Model(&job).Updates(map[string]any{
			"status": analysis.JobStatusSucceeded, "lease_until": nil,
			"last_error": "", "finished_at": now,
		}).Error
	})
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "AI任务不存在"})
			return
		}
		if errors.Is(err, errAIJobOwnership) {
			writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "保存AI任务结果失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) handleAIWorkerHeartbeat(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req aiWorkerHeartbeatRequest
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	req.WorkerID = strings.TrimSpace(req.WorkerID)
	if req.WorkerID == "" || len(req.WorkerID) > 100 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "worker_id不能为空且最多100个字符"})
		return
	}
	capabilities, _ := json.Marshal(cleanCapabilities(req.Capabilities))
	status := strings.TrimSpace(req.Status)
	if status == "" {
		status = "online"
	}
	if req.ActiveJobs < 0 {
		req.ActiveJobs = 0
	}
	worker := models.AIWorker{
		WorkerID: req.WorkerID, Hostname: req.Hostname, Version: req.Version,
		CapabilitiesJSON: string(capabilities), Status: status, ActiveJobs: req.ActiveJobs,
		LastError: req.LastError, HeartbeatAt: time.Now(),
	}
	err := database.DB.Clauses(clause.OnConflict{
		Columns: []clause.Column{{Name: "worker_id"}},
		DoUpdates: clause.AssignmentColumns([]string{
			"hostname", "version", "capabilities_json", "status", "active_jobs",
			"last_error", "heartbeat_at", "updated_at",
		}),
	}).Create(&worker).Error
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "保存AI Worker心跳失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

var errAIJobOwnership = errors.New("任务已被其他Worker领取或当前状态不可上报")

func (s *Server) validateFrameArtifacts(frames []aiFrameArtifact) error {
	root := filepath.Join(s.RecordingOutputDir, "_frames")
	seen := make(map[int]struct{}, len(frames))
	for _, frame := range frames {
		if frame.FrameIndex <= 0 {
			return errors.New("frame_index必须大于0")
		}
		if _, ok := seen[frame.FrameIndex]; ok {
			return errors.New("frame_index不能重复")
		}
		seen[frame.FrameIndex] = struct{}{}
		if frame.CapturedAt.IsZero() {
			return errors.New("captured_at不能为空")
		}
		if !pathIsWithin(root, frame.FilePath) {
			return errors.New("抽帧文件必须位于共享_frames目录中")
		}
		ext := strings.ToLower(filepath.Ext(frame.FilePath))
		if ext != ".jpg" && ext != ".jpeg" {
			return errors.New("抽帧文件必须是JPEG图片")
		}
	}
	return nil
}

func pathIsWithin(root, target string) bool {
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return false
	}
	targetAbs, err := filepath.Abs(target)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(rootAbs, targetAbs)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func cleanCapabilities(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.ToLower(strings.TrimSpace(value))
		if value == "" || len(value) > 50 {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}

func decodeJSONBody(r *http.Request, dst any) error {
	decoder := json.NewDecoder(io.LimitReader(r.Body, 2<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		return fmt.Errorf("JSON请求无效: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("JSON请求只能包含一个对象")
	}
	return nil
}
