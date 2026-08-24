package web

import (
	"encoding/json"
	"net/http"
	"time"

	"media-service/database"
)

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

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
