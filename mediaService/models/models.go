package models

import (
	"time"

	"gorm.io/gorm"
)

type Customer struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	Name      string    `gorm:"size:100;not null;uniqueIndex" json:"name"`
	Enabled   bool      `gorm:"not null;default:true;index" json:"enabled"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

type User struct {
	ID           uint      `gorm:"primaryKey" json:"id"`
	CustomerID   uint      `gorm:"not null;default:0;index" json:"customer_id"`
	Username     string    `gorm:"size:64;not null;uniqueIndex" json:"username"`
	PasswordHash string    `gorm:"size:100;not null" json:"-"`
	Role         string    `gorm:"size:30;not null;index" json:"role"`
	Enabled      bool      `gorm:"not null;default:true;index" json:"enabled"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

type UserSession struct {
	TokenHash string    `gorm:"size:64;primaryKey" json:"-"`
	UserID    uint      `gorm:"not null;index" json:"user_id"`
	ExpiresAt time.Time `gorm:"not null;index" json:"expires_at"`
	CreatedAt time.Time `json:"created_at"`
}

// VideoSource describes a vendor-neutral logical video point. Camera passwords
// and connection URLs stay on the client; only the operator and basic network
// identity needed by the management portals are synchronized to MediaService.
type VideoSource struct {
	ID           uint      `gorm:"primaryKey" json:"id"`
	CustomerID   uint      `gorm:"not null;default:0;index" json:"customer_id"`
	MAC          string    `gorm:"size:50;not null;uniqueIndex:idx_video_source_identity" json:"mac"`
	SourceType   string    `gorm:"size:30;not null;uniqueIndex:idx_video_source_identity" json:"source_type"`
	SourceID     string    `gorm:"size:100;not null;uniqueIndex:idx_video_source_identity" json:"source_id"`
	DisplayName  string    `gorm:"size:100;not null" json:"display_name"`
	OperatorName string    `gorm:"size:20" json:"operator_name"`
	Hostname     string    `gorm:"size:200" json:"hostname"`
	LocalIP      string    `gorm:"size:45" json:"local_ip"`
	Brand        string    `gorm:"size:100" json:"brand"`
	PublishMode  string    `gorm:"size:20;not null;default:app;index" json:"publish_mode"`
	Enabled      bool      `gorm:"not null;default:true;index" json:"enabled"`
	StreamName   string    `gorm:"size:64;not null;uniqueIndex" json:"stream_name"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

// VideoRecordingRule replaces the former global recording switch. Every
// source can independently enable MP4 storage and choose its retention hours.
type VideoRecordingRule struct {
	ID            uint      `gorm:"primaryKey" json:"id"`
	VideoSourceID uint      `gorm:"not null;uniqueIndex" json:"video_source_id"`
	Enabled       bool      `gorm:"not null;default:false;index" json:"enabled"`
	RetainHours   int       `gorm:"not null;default:48" json:"retain_hours"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
}

type RecordingSegment struct {
	ID         uint   `gorm:"primaryKey" json:"id"`
	CustomerID uint   `gorm:"not null;default:0;index" json:"customer_id"`
	StreamName string `gorm:"size:64;not null;index" json:"stream_name"`
	MAC        string `gorm:"size:64;index" json:"mac"`
	SourceType string `gorm:"size:30;not null;default:screen;index" json:"source_type"`
	SourceID   string `gorm:"size:100;not null;default:desktop" json:"source_id"`
	// 索引器每周期按 file_path 批量查重，必须建索引否则全表扫描。191 前缀、非唯一。
	// 使用固定索引名，保证 AutoMigrate 幂等。
	FilePath   string         `gorm:"size:500;not null;index:idx_recording_segments_file_path,length:191" json:"file_path"`
	FileSize   int64          `json:"file_size"`
	StartedAt  time.Time      `json:"started_at"`
	EndedAt    time.Time      `json:"ended_at"`
	Duration   float64        `json:"duration"`
	Storage    string         `gorm:"size:20;not null;default:local" json:"storage"`
	ServerURL  string         `gorm:"size:500" json:"server_url"`
	ServerName string         `gorm:"size:100" json:"server_name"`
	RemoteName string         `gorm:"size:500" json:"remote_name"`
	CreatedAt  time.Time      `json:"created_at"`
	DeletedAt  gorm.DeletedAt `gorm:"index" json:"deleted_at,omitempty"`
}

type RecordingFrame struct {
	ID         uint           `gorm:"primaryKey" json:"id"`
	CustomerID uint           `gorm:"not null;default:0;index" json:"customer_id"`
	StreamName string         `gorm:"size:64;not null;index" json:"stream_name"`
	MAC        string         `gorm:"size:64;index" json:"mac"`
	SourceType string         `gorm:"size:30;not null;default:screen;index" json:"source_type"`
	SourceID   string         `gorm:"size:100;not null;default:desktop" json:"source_id"`
	SegmentID  uint           `gorm:"index" json:"segment_id"`
	FilePath   string         `gorm:"size:500;not null" json:"file_path"`
	FileSize   int64          `json:"file_size"`
	CapturedAt time.Time      `json:"captured_at"`
	FrameIndex int            `json:"frame_index"`
	CreatedAt  time.Time      `json:"created_at"`
	DeletedAt  gorm.DeletedAt `gorm:"index" json:"deleted_at,omitempty"`
}

// AIAlgorithm is the catalog of analysis capabilities known by the platform.
// Algorithms can be implemented by different AIService versions without
// changing MediaService's event and scheduling model.
type AIAlgorithm struct {
	Code              string    `gorm:"size:50;primaryKey" json:"code"`
	Name              string    `gorm:"size:100;not null" json:"name"`
	Description       string    `gorm:"size:500" json:"description"`
	InputMode         string    `gorm:"size:20;not null" json:"input_mode"`
	Enabled           bool      `gorm:"not null;default:false;index" json:"enabled"`
	ModelVersion      string    `gorm:"size:100" json:"model_version"`
	DefaultConfigJSON string    `gorm:"type:text" json:"default_config_json"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
}

// VideoAnalysisRule controls which algorithms run for a specific video source.
// ConfigJSON stores algorithm-specific options such as sampling FPS, ROI,
// thresholds and alert cooldowns.
type VideoAnalysisRule struct {
	ID            uint      `gorm:"primaryKey" json:"id"`
	VideoSourceID uint      `gorm:"not null;uniqueIndex:idx_source_algorithm" json:"video_source_id"`
	AlgorithmCode string    `gorm:"size:50;not null;uniqueIndex:idx_source_algorithm;index" json:"algorithm_code"`
	Enabled       bool      `gorm:"not null;default:false;index" json:"enabled"`
	ConfigJSON    string    `gorm:"type:text" json:"config_json"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
}

// AIJob is a durable unit of work leased by AIService. The initial algorithm is
// frame_sampler; later offline algorithms can reuse the same queue.
type AIJob struct {
	ID            uint       `gorm:"primaryKey" json:"id"`
	JobKey        string     `gorm:"size:191;not null;uniqueIndex" json:"job_key"`
	AlgorithmCode string     `gorm:"size:50;not null;index:idx_ai_job_claim,priority:1" json:"algorithm_code"`
	InputType     string     `gorm:"size:30;not null" json:"input_type"`
	InputRefID    uint       `gorm:"not null;index" json:"input_ref_id"`
	Status        string     `gorm:"size:20;not null;index:idx_ai_job_claim,priority:2" json:"status"`
	Priority      int        `gorm:"not null;default:0;index:idx_ai_job_claim,priority:4" json:"priority"`
	Attempts      int        `gorm:"not null;default:0" json:"attempts"`
	MaxAttempts   int        `gorm:"not null;default:3" json:"max_attempts"`
	WorkerID      string     `gorm:"size:100;index" json:"worker_id"`
	LeaseUntil    *time.Time `gorm:"index" json:"lease_until"`
	AvailableAt   time.Time  `gorm:"not null;index:idx_ai_job_claim,priority:3" json:"available_at"`
	LastError     string     `gorm:"type:text" json:"last_error"`
	StartedAt     *time.Time `json:"started_at"`
	FinishedAt    *time.Time `json:"finished_at"`
	CreatedAt     time.Time  `json:"created_at"`
	UpdatedAt     time.Time  `json:"updated_at"`
}

// AIWorker records AIService process health and capabilities.
type AIWorker struct {
	WorkerID          string    `gorm:"size:100;primaryKey" json:"worker_id"`
	Hostname          string    `gorm:"size:200" json:"hostname"`
	Version           string    `gorm:"size:50" json:"version"`
	CapabilitiesJSON  string    `gorm:"type:text" json:"capabilities_json"`
	Status            string    `gorm:"size:20;not null;index" json:"status"`
	ActiveJobs        int       `gorm:"not null;default:0" json:"active_jobs"`
	ActiveStreams     int       `gorm:"not null;default:0" json:"active_streams"`
	DroppedFrames     int64     `gorm:"not null;default:0" json:"dropped_frames"`
	AnalyzerFailures  int64     `gorm:"not null;default:0" json:"analyzer_failures"`
	OpenCircuits      int       `gorm:"not null;default:0" json:"open_circuits"`
	UnassignedStreams int       `gorm:"not null;default:0" json:"unassigned_streams"`
	LastError         string    `gorm:"type:text" json:"last_error"`
	HeartbeatAt       time.Time `gorm:"index" json:"heartbeat_at"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
}

// AIEvent is the common result model for fight, helmet, fire and future event
// detectors. The frame_sampler module creates RecordingFrame rows instead.
type AIEvent struct {
	ID            uint       `gorm:"primaryKey" json:"id"`
	CustomerID    uint       `gorm:"not null;default:0;index" json:"customer_id"`
	EventID       string     `gorm:"size:64;not null;uniqueIndex" json:"event_id"`
	VideoSourceID uint       `gorm:"index" json:"video_source_id"`
	StreamName    string     `gorm:"size:64;not null;index" json:"stream_name"`
	AlgorithmCode string     `gorm:"size:50;not null;index" json:"algorithm_code"`
	EventType     string     `gorm:"size:50;not null;index" json:"event_type"`
	Confidence    float64    `json:"confidence"`
	StartedAt     time.Time  `gorm:"index" json:"started_at"`
	EndedAt       *time.Time `json:"ended_at"`
	SnapshotPath  string     `gorm:"size:500" json:"snapshot_path"`
	ClipPath      string     `gorm:"size:500" json:"clip_path"`
	ModelVersion  string     `gorm:"size:100" json:"model_version"`
	Status        string     `gorm:"size:20;not null;default:pending;index" json:"status"`
	MetadataJSON  string     `gorm:"type:text" json:"metadata_json"`
	CreatedAt     time.Time  `json:"created_at"`
	UpdatedAt     time.Time  `json:"updated_at"`
}
