package web

import (
	"crypto/subtle"
	"encoding/json"
	"net"
	"net/http"
	"strings"
	"time"

	"recording-service/database"
	"recording-service/models"

	"gorm.io/gorm/clause"
)

type clientRegistration struct {
	IP          string `json:"ip"`
	MAC         string `json:"mac"`
	Hostname    string `json:"hostname"`
	OS          string `json:"os"`
	CPU         string `json:"cpu"`
	CPUCores    int    `json:"cpu_cores"`
	TotalMemory int64  `json:"total_memory"`
	DiskSerial  string `json:"disk_serial"`
	Username    string `json:"username"`
	AppVersion  string `json:"app_version"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if database.DB == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "unavailable", "database": "not initialized", "time": time.Now()})
		return
	}
	sqlDB, err := database.DB.DB()
	if err != nil || sqlDB.PingContext(r.Context()) != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "unavailable", "database": "disconnected", "time": time.Now()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "database": "connected", "time": time.Now()})
}

func (s *Server) handleClientRegister(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.ClientAPIKey == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"message": "CLIENT_API_KEY 未配置"})
		return
	}
	provided := r.Header.Get("X-Client-Key")
	if len(provided) != len(s.ClientAPIKey) || subtle.ConstantTimeCompare([]byte(provided), []byte(s.ClientAPIKey)) != 1 {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "客户端密钥无效"})
		return
	}
	defer r.Body.Close()
	var input clientRegistration
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "请求数据无效: " + err.Error()})
		return
	}
	input.MAC = normalizeMAC(input.MAC)
	if !validMAC(input.MAC) {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "MAC 地址无效"})
		return
	}
	computer := models.Computer{
		IP: strings.TrimSpace(input.IP), MAC: input.MAC, Hostname: strings.TrimSpace(input.Hostname),
		OS: strings.TrimSpace(input.OS), CPU: strings.TrimSpace(input.CPU), TotalMemory: input.TotalMemory,
		DiskSerial: strings.TrimSpace(input.DiskSerial),
	}
	result := database.DB.Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "mac"}},
		DoUpdates: clause.AssignmentColumns([]string{"ip", "hostname", "os", "cpu", "total_memory", "disk_serial", "updated_at"}),
	}).Create(&computer)
	if result.Error != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "设备信息保存失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "mac": input.MAC})
}

func validMAC(value string) bool {
	parsed, err := net.ParseMAC(value)
	return err == nil && len(parsed) == 6
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
