package config

import (
	"log"
	"os"
	"strconv"
)

type Config struct {
	Database  DatabaseConfig
	Recording RecordingConfig
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
	RetainHours     int
	FrameRetainDays int
	FFmpegPath      string
	WebPort         int    // 内网管理后台 Web 页面端口，0=不启动
	SRSHttpHost     string // SRS HTTP-FLV/HLS 对外访问地址
	PublicRTMPHost  string // 客户端推流使用的公网 RTMP 地址
	AIStreamBaseURL string // AIService实时流兼容回退地址（通常为Docker内SRS HLS）
}

func Load() *Config {
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
			RetainHours:     recordingRetainHours(),
			FrameRetainDays: atoi("RECORDING_FRAME_RETAIN_DAYS", "30"),
			FFmpegPath:      getEnv("RECORDING_FFMPEG_PATH", "ffmpeg"),
			WebPort:         atoi("MEDIA_WEB_PORT", "22222"),
			SRSHttpHost:     getEnv("MEDIA_SRS_HTTP_HOST", ""),
			PublicRTMPHost:  getEnv("PUBLIC_RTMP_HOST", "112.18.238.6:1935"),
			AIStreamBaseURL: getEnv("AI_SRS_HTTP_BASE", "http://localhost:8080"),
		},
	}
}

func recordingRetainHours() int {
	hours := 48
	if _, ok := os.LookupEnv("RECORDING_RETAIN_HOURS"); ok {
		hours = atoi("RECORDING_RETAIN_HOURS", "48")
	} else if _, ok := os.LookupEnv("RECORDING_RETAIN_DAYS"); ok {
		// Accept the former day-based deployment variable during upgrades.
		hours = atoi("RECORDING_RETAIN_DAYS", "7") * 24
	}
	if hours <= 0 {
		log.Printf("[config] 录像默认保留小时数必须大于0，使用默认值48")
		return 48
	}
	return hours
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
