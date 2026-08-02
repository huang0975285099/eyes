package models

import (
	"time"

	"gorm.io/gorm"
)

type Region struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	Name      string    `gorm:"size:100;not null" json:"name"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	// DeletedAt 使 GORM 自动过滤软删除的 Region。
	DeletedAt gorm.DeletedAt `gorm:"index" json:"deleted_at,omitempty"`
}

type Area struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	Name      string    `gorm:"size:100;not null" json:"name"`
	RegionID  uint      `json:"region_id"`
	Region    Region    `gorm:"foreignKey:RegionID" json:"region,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	// DeletedAt 使 GORM 自动过滤软删除的 Area。
	DeletedAt gorm.DeletedAt `gorm:"index" json:"deleted_at,omitempty"`
}

type Zone struct {
	ID     uint   `gorm:"primaryKey" json:"id"`
	Name   string `gorm:"size:100;not null" json:"name"`
	AreaID uint   `json:"area_id"`
	Area   Area   `gorm:"foreignKey:AreaID" json:"area,omitempty"`
	// RecordEnabled 该车间是否开启服务端录制。默认 true 保持旧行为。
	// 关闭后该车间设备推流照常、可实时监控，但服务端不录制、不落盘、不转存到接收机。
	RecordEnabled bool      `gorm:"not null;default:true" json:"record_enabled"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
	// DeletedAt 使 GORM 自动过滤软删除的 Zone。
	DeletedAt gorm.DeletedAt `gorm:"index" json:"deleted_at,omitempty"`
}

// ZoneAssignment 节点-车间分配关系，用于在后台管理各内网节点录制的车间。
// 一个节点可分配多个车间（多对多）；启动时若 DB 中存在当前节点的记录，
// 将覆盖环境变量 RECORDING_NODE_ZONE_ID 的值。
type ZoneAssignment struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	NodeID    string    `gorm:"size:100;not null;uniqueIndex:idx_node_zone,priority:1" json:"node_id"`
	NodeName  string    `gorm:"size:200" json:"node_name"`
	ZoneID    uint      `gorm:"not null;uniqueIndex:idx_node_zone,priority:2" json:"zone_id"`
	Zone      Zone      `gorm:"foreignKey:ZoneID" json:"zone,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// NodeSetting 节点级配置，存储可后台修改的参数（如录像保留天数）。
// 启动时若 DB 中存在当前节点的记录，将覆盖环境变量的值。
type NodeSetting struct {
	ID         uint      `gorm:"primaryKey" json:"id"`
	NodeID     string    `gorm:"size:100;not null;uniqueIndex" json:"node_id"`
	RetainDays int       `gorm:"not null;default:7" json:"retain_days"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

type User struct {
	ID      uint   `gorm:"primaryKey"`
	Account string `gorm:"size:50"`
	Name    string `gorm:"size:100"`
	ZoneID  *uint
	Zone    Zone `gorm:"foreignKey:ZoneID"`
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
	NodeZoneID uint           `gorm:"index;default:0" json:"node_zone_id"`
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
	NodeZoneID uint           `gorm:"index;default:0" json:"node_zone_id"`
	CreatedAt  time.Time      `json:"created_at"`
	DeletedAt  gorm.DeletedAt `gorm:"index" json:"deleted_at,omitempty"`
}
