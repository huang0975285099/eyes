package config

import (
	"log"
	"os"
	"strconv"
)

type Config struct {
	Database  DatabaseConfig
	Recording RecordingConfig
	Security  SecurityConfig
}

type SecurityConfig struct {
	ClientAPIKey string
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
	WebPort         int    // 内网管理后台 Web 页面端口，0=不启动
	SRSHttpHost     string // SRS HTTP-FLV/HLS 对外访问地址
	PublicRTMPHost  string // 客户端推流使用的公网 RTMP 地址
}

func Load() *Config {
	clientAPIKey := getEnv("CLIENT_API_KEY", "")
	return &Config{
		Database: DatabaseConfig{
			Host:     getEnv("DB_HOST", "localhost"),
			Port:     atoi("DB_PORT", "3306"),
			User:     getEnv("DB_USER", "root"),
			Password: getEnv("DB_PASSWORD", "all_seeing_eyes"),
			DBName:   getEnv("DB_NAME", "eyes"),
			Charset:  getEnv("DB_CHARSET", "utf8mb4"),
		},
		Recording: RecordingConfig{
			SRSApiBase:      getEnv("RECORDING_SRS_API", "http://localhost:1985"),
			RTMPHost:        getEnv("RECORDING_RTMP_HOST", "localhost"),
			OutputDir:       getEnv("RECORDING_OUTPUT_DIR", "/var/recordings"),
			SegmentDuration: atoi("RECORDING_SEGMENT_DURATION", "600"),
			CheckInterval:   atoi("RECORDING_CHECK_INTERVAL", "30"),
			RetainDays:      atoi("RECORDING_RETAIN_DAYS", "7"),
			FrameRetainDays: atoi("RECORDING_FRAME_RETAIN_DAYS", "30"),
			FFmpegPath:      getEnv("RECORDING_FFMPEG_PATH", "ffmpeg"),
			WebPort:         atoi("MEDIA_WEB_PORT", "22222"),
			SRSHttpHost:     getEnv("MEDIA_SRS_HTTP_HOST", ""),
			PublicRTMPHost:  getEnv("PUBLIC_RTMP_HOST", "112.18.238.6:1935"),
		},
		Security: SecurityConfig{
			ClientAPIKey: clientAPIKey,
		},
	}
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
