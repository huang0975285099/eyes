package web

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"recording-service/database"
	"recording-service/models"
	"strconv"
	"strings"
	"time"
)

//go:embed index.html
var indexHTML []byte

//go:embed hls.min.js
var hlsJS []byte

type Server struct {
	SRSApiBase          string     // SRS HTTP API 地址（如 http://127.0.0.1:1985）
	SRSHttpHost         string     // SRS HTTP-FLV/HLS 对外地址（如 10.0.20.219:8090）
	RetainDays          int        // 录像保留天数（可被后台修改）
	EnvRetainDays       int        // 环境变量初始值，DB 无配置时回退
	RecordEnabled       bool       // 全局录制开关
	UpdateRetainDays    func(int)  // 热更新 RecorderManager 的回调
	UpdateRecordEnabled func(bool) // 热更新全局录制开关
	ClientAPIKey        string     // Electron 设备登记接口共享密钥
	PublicRTMPHost      string     // 下发给客户端的 RTMP 公网地址
	StreamSecret        string     // SRS 发布 token HMAC 密钥
}

func NewServer(srsApiBase, srsHttpHost, publicRTMPHost string, retainDays int, recordEnabled bool, updateRetainDays func(int), updateRecordEnabled func(bool), clientAPIKey, streamSecret string) *Server {
	return &Server{
		SRSApiBase:          srsApiBase,
		SRSHttpHost:         srsHttpHost,
		RetainDays:          retainDays,
		EnvRetainDays:       retainDays,
		RecordEnabled:       recordEnabled,
		UpdateRetainDays:    updateRetainDays,
		UpdateRecordEnabled: updateRecordEnabled,
		ClientAPIKey:        clientAPIKey,
		PublicRTMPHost:      publicRTMPHost,
		StreamSecret:        streamSecret,
	}
}

func (s *Server) Start(port int) {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/hls.min.js", s.handleHlsJS)
	mux.HandleFunc("/api/segments", s.handleSegments)
	mux.HandleFunc("/api/frames", s.handleFrames)
	mux.HandleFunc("/api/macs", s.handleMACs)
	mux.HandleFunc("/api/streams", s.handleStreams)
	mux.HandleFunc("/api/recording-settings", s.handleRecordingSettings)
	mux.HandleFunc("/api/stats", s.handleStats)
	mux.HandleFunc("/api/health", s.handleHealth)
	mux.HandleFunc("/api/clients/register", s.handleClientRegister)
	mux.HandleFunc("/api/client-updates/latest", s.handleClientUpdateLatest)
	mux.HandleFunc("/api/client-updates/upload", s.handleClientUpdateUpload)
	mux.Handle("/client-updates/", http.StripPrefix("/client-updates/", http.FileServer(http.Dir(updateDir()))))
	mux.HandleFunc("/api/streams/publish-config", s.handlePublishConfig)
	mux.HandleFunc("/api/srs/on-publish", s.handleSRSPublish)
	mux.HandleFunc("/api/srs/on-unpublish", s.handleSRSLifecycle)
	mux.HandleFunc("/api/srs/on-play", s.handleSRSLifecycle)
	mux.HandleFunc("/api/srs/on-stop", s.handleSRSLifecycle)
	mux.HandleFunc("/segments/", s.handleVideo)
	mux.HandleFunc("/frames/", s.handleFrameImage)

	addr := fmt.Sprintf(":%d", port)
	log.Printf("[web] 内网管理后台：http://0.0.0.0%s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Printf("[web] HTTP 服务器退出: %v", err)
	}
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(indexHTML)
}

func (s *Server) handleHlsJS(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=86400")
	w.Write(hlsJS)
}

// ============================================================
// 推流状态
// ============================================================

type streamStatusRow struct {
	StreamName string `json:"stream_name"`
	MAC        string `json:"mac"`
	IP         string `json:"ip"`
	PublicIP   string `json:"public_ip"`
	Hostname   string `json:"hostname"`
	UserName   string `json:"user_name"`
	Active     bool   `json:"active"`
}

func (s *Server) handleStreams(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// 查询 SRS 活跃流
	streams := s.fetchSRSStreams()

	// 批量查设备信息
	macSet := map[string]struct{}{}
	for _, st := range streams {
		mac := streamNameToMAC(st.Name)
		if mac != "" {
			macSet[mac] = struct{}{}
		}
	}

	type compRow struct {
		MAC      string
		IP       string
		PublicIP string
		Hostname string
		UserName string
	}
	compMap := map[string]compRow{}
	if len(macSet) > 0 {
		macs := make([]string, 0, len(macSet))
		for m := range macSet {
			macs = append(macs, m)
		}
		var comps []compRow
		database.DB.Table("computers").
			Select("computers.mac, computers.ip, computers.public_ip, computers.hostname, computers.user_name").
			Where("computers.mac IN ?", macs).
			Scan(&comps)
		for _, c := range comps {
			compMap[c.MAC] = c
		}
	}

	rows := make([]streamStatusRow, 0, len(streams))
	for _, st := range streams {
		mac := streamNameToMAC(st.Name)
		row := streamStatusRow{
			StreamName: st.Name,
			MAC:        mac,
			Active:     st.Publish.Active,
		}
		if c, ok := compMap[mac]; ok {
			row.IP = c.IP
			row.PublicIP = c.PublicIP
			row.Hostname = c.Hostname
			row.UserName = c.UserName
		}
		rows = append(rows, row)
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(rows)
}

type srsStream struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	App     string `json:"app"`
	Publish struct {
		Active bool `json:"active"`
	} `json:"publish"`
}

func (s *Server) fetchSRSStreams() []srsStream {
	if s.SRSApiBase == "" {
		return nil
	}
	url := strings.TrimRight(s.SRSApiBase, "/") + "/api/v1/streams/?count=100000"
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		log.Printf("[web] 查询 SRS 流列表失败: %v", err)
		return nil
	}
	defer resp.Body.Close()

	var body struct {
		Code    int         `json:"code"`
		Streams []srsStream `json:"streams"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil
	}
	if body.Code != 0 {
		return nil
	}

	seen := map[string]struct{}{}
	result := body.Streams[:0]
	for _, st := range body.Streams {
		st.Name = strings.TrimSuffix(st.Name, ".flv")
		if st.Name == "" || !st.Publish.Active {
			continue
		}
		if _, dup := seen[st.Name]; dup {
			continue
		}
		seen[st.Name] = struct{}{}
		result = append(result, st)
	}
	return result
}

// streamNameToMAC 将流名（如 d85ed39f2a17）转为 MAC 格式（d8:5e:d3:9f:2a:17）
func streamNameToMAC(name string) string {
	name = strings.ToLower(strings.TrimSpace(name))
	if len(name) != 12 {
		return name
	}
	var b strings.Builder
	for i := 0; i < 12; i += 2 {
		if i > 0 {
			b.WriteByte(':')
		}
		b.WriteString(name[i : i+2])
	}
	return b.String()
}

// ============================================================
// 录制设置（录像保留天数等，可后台修改）
// ============================================================

func (s *Server) handleRecordingSettings(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		s.getRecordingSettings(w, r)
	case http.MethodPut:
		s.updateRecordingSettings(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) getRecordingSettings(w http.ResponseWriter, r *http.Request) {
	var setting models.RecordingSetting
	hasDBConfig := false
	if err := database.DB.First(&setting).Error; err == nil {
		hasDBConfig = true
	}
	resp := map[string]interface{}{
		"retain_days":     s.RetainDays,
		"record_enabled":  s.RecordEnabled,
		"env_retain_days": s.EnvRetainDays,
		"using_db_config": hasDBConfig,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (s *Server) updateRecordingSettings(w http.ResponseWriter, r *http.Request) {
	var req struct {
		RetainDays    *int  `json:"retain_days"`
		RecordEnabled *bool `json:"record_enabled"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "参数错误", http.StatusBadRequest)
		return
	}
	if req.RetainDays != nil && *req.RetainDays <= 0 {
		http.Error(w, "retain_days 必须大于 0", http.StatusBadRequest)
		return
	}
	if req.RetainDays == nil && req.RecordEnabled == nil {
		http.Error(w, "无更新字段", http.StatusBadRequest)
		return
	}
	updates := map[string]interface{}{}
	if req.RetainDays != nil {
		updates["retain_days"] = *req.RetainDays
	}
	if req.RecordEnabled != nil {
		updates["record_enabled"] = *req.RecordEnabled
	}

	// Upsert：有则更新，无则创建
	var setting models.RecordingSetting
	if err := database.DB.First(&setting).Error; err != nil {
		setting = models.RecordingSetting{RetainDays: s.RetainDays, RecordEnabled: s.RecordEnabled}
		if req.RetainDays != nil {
			setting.RetainDays = *req.RetainDays
		}
		if req.RecordEnabled != nil {
			setting.RecordEnabled = *req.RecordEnabled
		}
		if err := database.DB.Create(&setting).Error; err != nil {
			http.Error(w, fmt.Sprintf("保存失败: %v", err), http.StatusInternalServerError)
			return
		}
		if err := database.DB.Model(&setting).Updates(updates).Error; err != nil {
			http.Error(w, fmt.Sprintf("保存失败: %v", err), http.StatusInternalServerError)
			return
		}
	} else {
		if err := database.DB.Model(&setting).Updates(updates).Error; err != nil {
			http.Error(w, fmt.Sprintf("保存失败: %v", err), http.StatusInternalServerError)
			return
		}
	}

	// 热更新内存值
	if req.RetainDays != nil {
		s.RetainDays = *req.RetainDays
		if s.UpdateRetainDays != nil {
			s.UpdateRetainDays(*req.RetainDays)
		}
	}
	if req.RecordEnabled != nil {
		s.RecordEnabled = *req.RecordEnabled
		if s.UpdateRecordEnabled != nil {
			s.UpdateRecordEnabled(*req.RecordEnabled)
		}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"ok": true})
}

// ============================================================
// 统计信息
// ============================================================

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// 录像统计
	var segCount int64
	var totalSize int64
	statsQuery := database.DB.Model(&models.RecordingSegment{}).Where("storage = ?", "local")
	statsQuery.Count(&segCount)

	statsQuery2 := database.DB.Model(&models.RecordingSegment{}).Where("storage = ?", "local")
	statsQuery2.Select("COALESCE(SUM(file_size), 0)").Scan(&totalSize)

	// 磁盘使用率
	diskTotal, diskFree := diskUsage(".")
	diskUsed := diskTotal - diskFree
	diskPercent := 0.0
	if diskTotal > 0 {
		diskPercent = float64(diskUsed) / float64(diskTotal) * 100
	}

	onlineStreams := len(s.fetchSRSStreams())

	resp := map[string]interface{}{
		"seg_count":      segCount,
		"total_size":     totalSize,
		"online_streams": onlineStreams,
		"disk_total":     diskTotal,
		"disk_free":      diskFree,
		"disk_used":      diskUsed,
		"disk_percent":   diskPercent,
		"srs_http_host":  s.SRSHttpHost,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// ============================================================
// 录像列表（原有功能）
// ============================================================

type segmentRow struct {
	ID         uint      `json:"id"`
	StreamName string    `json:"stream_name"`
	MAC        string    `json:"mac"`
	Hostname   string    `json:"hostname"`
	UserName   string    `json:"user_name"`
	StartedAt  time.Time `json:"started_at"`
	EndedAt    time.Time `json:"ended_at"`
	Duration   float64   `json:"duration"`
	FileSize   int64     `json:"file_size"`
}

type frameRow struct {
	ID         uint      `json:"id"`
	StreamName string    `json:"stream_name"`
	MAC        string    `json:"mac"`
	CapturedAt time.Time `json:"captured_at"`
	FrameIndex int       `json:"frame_index"`
	FileSize   int64     `json:"file_size"`
}

func (s *Server) handleFrames(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	query := database.DB.Model(&models.RecordingFrame{})
	if dateStr := r.URL.Query().Get("date"); dateStr != "" {
		if t, err := time.ParseInLocation("2006-01-02", dateStr, time.Local); err == nil {
			query = query.Where("captured_at >= ? AND captured_at < ?", t, t.AddDate(0, 0, 1))
		}
	}
	if mac := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("mac"))); mac != "" {
		query = query.Where("mac = ?", mac)
	}
	var frames []models.RecordingFrame
	query.Order("captured_at DESC").Limit(500).Find(&frames)
	rows := make([]frameRow, 0, len(frames))
	for _, frame := range frames {
		rows = append(rows, frameRow{
			ID: frame.ID, StreamName: frame.StreamName, MAC: frame.MAC,
			CapturedAt: frame.CapturedAt, FrameIndex: frame.FrameIndex, FileSize: frame.FileSize,
		})
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(rows)
}

func (s *Server) handleFrameImage(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/frames/")
	parts := strings.SplitN(path, "/", 2)
	if len(parts) != 2 || parts[1] != "image" {
		http.NotFound(w, r)
		return
	}
	id, err := strconv.ParseUint(parts[0], 10, 64)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	var frame models.RecordingFrame
	if err := database.DB.First(&frame, id).Error; err != nil {
		http.NotFound(w, r)
		return
	}
	f, err := os.Open(frame.FilePath)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	http.ServeContent(w, r, filepath.Base(frame.FilePath), fi.ModTime(), f)
}

func (s *Server) handleSegments(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	q := r.URL.Query()
	dateStr := q.Get("date")
	mac := q.Get("mac")

	query := database.DB.Model(&models.RecordingSegment{}).
		Where("storage = ?", "local")

	if dateStr != "" {
		t, err := time.ParseInLocation("2006-01-02", dateStr, time.Local)
		if err == nil {
			query = query.Where("started_at >= ? AND started_at < ?", t, t.AddDate(0, 0, 1))
		}
	}
	if mac != "" {
		query = query.Where("mac = ?", strings.ToLower(mac))
	}

	var segs []models.RecordingSegment
	query.Order("started_at DESC").Limit(500).Find(&segs)

	// 批量查设备信息
	macSet := map[string]struct{}{}
	for _, seg := range segs {
		if seg.MAC != "" {
			macSet[seg.MAC] = struct{}{}
		}
	}
	type compRow struct {
		MAC      string
		Hostname string
		Name     string
	}
	compMap := map[string]compRow{}
	if len(macSet) > 0 {
		macs := make([]string, 0, len(macSet))
		for m := range macSet {
			macs = append(macs, m)
		}
		var comps []compRow
		database.DB.Table("computers").
			Select("computers.mac, computers.hostname, users.name").
			Joins("LEFT JOIN users ON users.id = computers.user_id").
			Where("computers.mac IN ?", macs).
			Scan(&comps)
		for _, c := range comps {
			compMap[c.MAC] = c
		}
	}

	rows := make([]segmentRow, 0, len(segs))
	for _, seg := range segs {
		row := segmentRow{
			ID:         seg.ID,
			StreamName: seg.StreamName,
			MAC:        seg.MAC,
			StartedAt:  seg.StartedAt,
			EndedAt:    seg.EndedAt,
			Duration:   seg.Duration,
			FileSize:   seg.FileSize,
		}
		if c, ok := compMap[seg.MAC]; ok {
			row.Hostname = c.Hostname
			row.UserName = c.Name
		}
		rows = append(rows, row)
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(rows)
}

type macInfo struct {
	MAC      string `json:"mac"`
	Hostname string `json:"hostname"`
	UserName string `json:"user_name"`
}

func (s *Server) handleMACs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var macs []string
	macQuery := database.DB.Model(&models.RecordingSegment{}).Where("storage = ?", "local")
	macQuery.Distinct("mac").Pluck("mac", &macs)

	result := make([]macInfo, 0, len(macs))
	if len(macs) > 0 {
		type compRow struct {
			MAC      string
			Hostname string
			Name     string
		}
		var comps []compRow
		database.DB.Table("computers").
			Select("computers.mac, computers.hostname, users.name").
			Joins("LEFT JOIN users ON users.id = computers.user_id").
			Where("computers.mac IN ?", macs).
			Scan(&comps)

		found := map[string]bool{}
		for _, c := range comps {
			result = append(result, macInfo{MAC: c.MAC, Hostname: c.Hostname, UserName: c.Name})
			found[c.MAC] = true
		}
		for _, m := range macs {
			if !found[m] {
				result = append(result, macInfo{MAC: m})
			}
		}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

// handleVideo 提供 MP4 文件的 HTTP 流式播放，支持 Range 请求（浏览器拖动进度条）。
// 路径格式：/segments/{id}/video
func (s *Server) handleVideo(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/segments/")
	parts := strings.SplitN(path, "/", 2)
	if len(parts) != 2 || parts[1] != "video" {
		http.NotFound(w, r)
		return
	}

	id, err := strconv.ParseUint(parts[0], 10, 64)
	if err != nil {
		http.NotFound(w, r)
		return
	}

	var seg models.RecordingSegment
	segQuery := database.DB.Where("id = ?", id)
	if err := segQuery.First(&seg).Error; err != nil {
		http.NotFound(w, r)
		return
	}

	f, err := os.Open(seg.FilePath)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer f.Close()

	fi, err := f.Stat()
	if err != nil {
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Disposition", "inline; filename=\""+filepath.Base(seg.FilePath)+"\"")
	http.ServeContent(w, r, filepath.Base(seg.FilePath), fi.ModTime(), f)
}
