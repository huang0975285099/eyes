package config

import (
	"log"
	"os"
	"strconv"
	"strings"
)

type Config struct {
	Database  DatabaseConfig
	Recording RecordingConfig
	Security  SecurityConfig
}

type SecurityConfig struct {
	ClientAPIKey      string
	StreamTokenSecret string
}

type DatabaseConfig struct {
	Host     string
	Port     int
	User     string
	Password string
	DBName   string
	Charset  string
}

type RecordingConfig struct {
	SRSApiBase      string
	RTMPHost        string
	OutputDir       string
	SegmentDuration int
	CheckInterval   int
	RetainDays      int
	FrameRetainDays int
	FFmpegPath      string
	NodeZoneIDs     []uint // 录制的车间 ID 列表；空或含 0 = 录制所有车间（环境变量初始值）
	CenterURL       string // A节点 API 地址，用于推送抽帧；空=本地存储
	CenterKey       string // A节点 X-Proxy-Key 值
	WebPort         int    // 内网管理后台 Web 页面端口，0=不启动
	SRSHttpHost     string // SRS HTTP-FLV/HLS 对外访问地址（如 10.0.20.219:28080）
	PublicRTMPHost  string // 客户端推流使用的公网 RTMP 地址（如 112.18.238.6:21935）
	// NodeID 本节点唯一标识，用于 zone_assignments 表关联。
	// 优先读 RECORDING_NODE_ID 环境变量，为空则使用 hostname。
	// 后台修改车间分配后，按 NodeID 匹配并热更新，无需重启。
	NodeID string
	// NodeName 节点可读名称（如"眉州监狱-内网节点"），仅用于后台展示。
	NodeName string
}

func Load() *Config {
	clientAPIKey := getEnv("CLIENT_API_KEY", "")
	streamTokenSecret := getEnv("STREAM_TOKEN_SECRET", "")
	if streamTokenSecret == "" {
		streamTokenSecret = clientAPIKey
	}
	return &Config{
		Database: DatabaseConfig{
			Host:     getEnv("DB_HOST", "localhost"),
			Port:     atoi("DB_PORT", "3306"),
			User:     getEnv("DB_USER", "root"),
			Password: getEnv("DB_PASSWORD", ""),
			DBName:   getEnv("DB_NAME", "user_management"),
			Charset:  getEnv("DB_CHARSET", "utf8mb4"),
		},
		Recording: RecordingConfig{
			SRSApiBase:      getEnv("RECORDING_SRS_API", "http://localhost:21985"),
			RTMPHost:        getEnv("RECORDING_RTMP_HOST", "localhost"),
			OutputDir:       getEnv("RECORDING_OUTPUT_DIR", "/var/recordings"),
			SegmentDuration: atoi("RECORDING_SEGMENT_DURATION", "600"),
			CheckInterval:   atoi("RECORDING_CHECK_INTERVAL", "30"),
			RetainDays:      atoi("RECORDING_RETAIN_DAYS", "7"),
			FrameRetainDays: atoi("RECORDING_FRAME_RETAIN_DAYS", "30"),
			FFmpegPath:      getEnv("RECORDING_FFMPEG_PATH", "ffmpeg"),
			NodeZoneIDs:     parseZoneIDs(getEnv("RECORDING_NODE_ZONE_ID", "0")),
			CenterURL:       getEnv("RECORDING_CENTER_URL", ""),
			CenterKey:       getEnv("RECORDING_CENTER_KEY", ""),
			WebPort:         atoi("RECORDING_WEB_PORT", "8089"),
			SRSHttpHost:     getEnv("RECORDING_SRS_HTTP_HOST", ""),
			PublicRTMPHost:  getEnv("PUBLIC_RTMP_HOST", "112.18.238.6:21935"),
			NodeID:          getEnv("RECORDING_NODE_ID", ""),
			NodeName:        getEnv("RECORDING_NODE_NAME", ""),
		},
		Security: SecurityConfig{
			ClientAPIKey:      clientAPIKey,
			StreamTokenSecret: streamTokenSecret,
		},
	}
}

// parseZoneIDs 解析逗号分隔的 Zone ID 列表，如 "1,2" → [1, 2]，"0" → [0]
func parseZoneIDs(s string) []uint {
	parts := strings.Split(s, ",")
	ids := make([]uint, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		n, err := strconv.Atoi(p)
		if err != nil {
			log.Printf("[config] RECORDING_NODE_ZONE_ID 包含无效值 %q，已跳过", p)
			continue
		}
		ids = append(ids, uint(n))
	}
	if len(ids) == 0 {
		ids = []uint{0}
	}
	return ids
}

func getEnv(key, def string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return def
}

func atoi(key, def string) int {
	s := getEnv(key, def)
	n, err := strconv.Atoi(s)
	if err != nil {
		log.Printf("[config] %s=%q 不是有效整数，使用默认值 %s", key, s, def)
		n, _ = strconv.Atoi(def)
	}
	return n
}
