package web

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const streamTokenTTL = 24 * time.Hour

type publishConfigRequest struct {
	MAC string `json:"mac"`
}

type srsHookRequest struct {
	Action string `json:"action"`
	App    string `json:"app"`
	Stream string `json:"stream"`
	Param  string `json:"param"`
}

func (s *Server) handlePublishConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !s.authorizeClient(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "客户端密钥无效"})
		return
	}
	if s.PublicRTMPHost == "" || s.StreamSecret == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"message": "推流服务未配置"})
		return
	}
	defer r.Body.Close()
	var input publishConfigRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<10)).Decode(&input); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "请求数据无效"})
		return
	}
	mac := normalizeMAC(input.MAC)
	if !validMAC(mac) {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "MAC 地址无效"})
		return
	}
	streamName := strings.ReplaceAll(mac, ":", "")
	expiresAt := time.Now().Add(streamTokenTTL).Unix()
	token := signStreamToken(s.StreamSecret, streamName, expiresAt)
	rtmpURL := fmt.Sprintf("rtmp://%s/live/%s?token=%s", s.PublicRTMPHost, streamName, url.QueryEscape(token))
	playbackURL := ""
	if s.SRSHttpHost != "" {
		playbackURL = fmt.Sprintf("http://%s/live/%s.flv", s.SRSHttpHost, streamName)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"rtmp_url": rtmpURL, "stream_name": streamName,
		"playback_url": playbackURL, "expires_at": expiresAt,
	})
}

func (s *Server) handleSRSPublish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	defer r.Body.Close()
	var hook srsHookRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&hook); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 1, "message": "invalid hook"})
		return
	}
	params, _ := url.ParseQuery(strings.TrimPrefix(hook.Param, "?"))
	if hook.App != "live" || !verifyStreamToken(s.StreamSecret, hook.Stream, params.Get("token"), time.Now()) {
		writeJSON(w, http.StatusOK, map[string]any{"code": 1, "message": "unauthorized stream"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

func (s *Server) handleSRSLifecycle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

func (s *Server) authorizeClient(r *http.Request) bool {
	provided := r.Header.Get("X-Client-Key")
	return s.ClientAPIKey != "" && hmac.Equal([]byte(provided), []byte(s.ClientAPIKey))
}

func normalizeMAC(value string) string {
	return strings.ToLower(strings.ReplaceAll(strings.TrimSpace(value), "-", ":"))
}

func signStreamToken(secret, stream string, expiry int64) string {
	expiryText := strconv.FormatInt(expiry, 10)
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(stream + "\n" + expiryText))
	return expiryText + "." + hex.EncodeToString(mac.Sum(nil))
}

func verifyStreamToken(secret, stream, token string, now time.Time) bool {
	parts := strings.Split(token, ".")
	if secret == "" || len(parts) != 2 {
		return false
	}
	expiry, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || now.Unix() > expiry {
		return false
	}
	expected := signStreamToken(secret, stream, expiry)
	return hmac.Equal([]byte(expected), []byte(token))
}
