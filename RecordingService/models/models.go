package models

import (
	"time"

	"gorm.io/gorm"
)

// RecordingSetting 存储可在后台修改的全局录制参数。
type RecordingSetting struct {
	ID            uint      `gorm:"primaryKey" json:"id"`
	RetainDays    int       `gorm:"not null;default:7" json:"retain_days"`
	RecordEnabled bool      `gorm:"not null;default:true" json:"record_enabled"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
}

type User struct {
	ID      uint   `gorm:"primaryKey"`
	Account string `gorm:"size:50"`
	Name    string `gorm:"size:100"`
}

type Computer struct {
	ID          uint   `gorm:"primaryKey"`
	IP          string `gorm:"size:50"`
	MAC         string `gorm:"size:50;uniqueIndex"`
	OS          string `gorm:"size:200"`
	Hostname    string `gorm:"size:100"`
	CPU         string `gorm:"size:200"`
	TotalMemory int64
	DiskSerial  string `gorm:"size:200"`
	UserID      *uint  `gorm:"uniqueIndex"`
	User        User   `gorm:"foreignKey:UserID"`
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

type RecordingSegment struct {
	ID         uint   `gorm:"primaryKey" json:"id"`
	StreamName string `gorm:"size:64;not null;index" json:"stream_name"`
	MAC        string `gorm:"size:64;index" json:"mac"`
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
	StreamName string         `gorm:"size:64;not null;index" json:"stream_name"`
	MAC        string         `gorm:"size:64;index" json:"mac"`
	SegmentID  uint           `gorm:"index" json:"segment_id"`
	FilePath   string         `gorm:"size:500;not null" json:"file_path"`
	FileSize   int64          `json:"file_size"`
	Tag        string         `gorm:"size:20;default:''" json:"tag"`
	CapturedAt time.Time      `json:"captured_at"`
	FrameIndex int            `json:"frame_index"`
	CreatedAt  time.Time      `json:"created_at"`
	DeletedAt  gorm.DeletedAt `gorm:"index" json:"deleted_at,omitempty"`
}
