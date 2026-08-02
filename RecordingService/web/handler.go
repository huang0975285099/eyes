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
	"recording-service/nodeconfig"
	"strconv"
	"strings"
	"time"
)

//go:embed index.html
var indexHTML []byte

//go:embed hls.min.js
var hlsJS []byte

type Server struct {
	ZoneCfg          *nodeconfig.ZoneConfig // 与 RecorderManager 共享，支持热更新
	NodeID           string                 // 本节点唯一标识
	NodeName         string                 // 节点可读名称
	EnvZoneIDs       []uint                 // 环境变量初始值，DB 无配置时回退
	SRSApiBase       string                 // SRS HTTP API 地址（如 http://127.0.0.1:21985）
	SRSHttpHost      string                 // SRS HTTP-FLV/HLS 对外地址（如 10.0.20.219:28080）
	RetainDays       int                    // 录像保留天数（可被后台修改）
	EnvRetainDays    int                    // 环境变量初始值，DB 无配置时回退
	UpdateRetainDays func(int)              // 热更新 RecorderManager 的回调
	ClientAPIKey     string                 // Electron 设备登记接口共享密钥
	PublicRTMPHost   string                 // 下发给客户端的 RTMP 公网地址
	StreamSecret     string                 // SRS 发布 token HMAC 密钥
}

func NewServer(zoneCfg *nodeconfig.ZoneConfig, nodeID, nodeName string, envZoneIDs []uint, srsApiBase, srsHttpHost, publicRTMPHost string, retainDays int, updateRetainDays func(int), clientAPIKey, streamSecret string) *Server {
	return &Server{
		ZoneCfg:          zoneCfg,
		NodeID:           nodeID,
		NodeName:         nodeName,
		EnvZoneIDs:       envZoneIDs,
		SRSApiBase:       srsApiBase,
		SRSHttpHost:      srsHttpHost,
		RetainDays:       retainDays,
		EnvRetainDays:    retainDays,
		UpdateRetainDays: updateRetainDays,
		ClientAPIKey:     clientAPIKey,
		PublicRTMPHost:   publicRTMPHost,
		StreamSecret:     streamSecret,
	}
}

// isAllZones 返回 true 表示录制所有车间。
func (s *Server) isAllZones() bool {
	return s.ZoneCfg.IsAllZones()
}

// zoneAllowed 返回 true 表示该 Zone 在本节点的负责范围内。
func (s *Server) zoneAllowed(zoneID uint) bool {
	return s.ZoneCfg.ZoneAllowed(zoneID)
}

// zoneFilter 返回 SQL WHERE 条件片段和参数，用于按 NodeZoneIDs 过滤。
// 全 Zone 模式时返回空字符串（不过滤）。
func (s *Server) zoneFilter() (string, []interface{}) {
	return s.ZoneCfg.ZoneCond()
}

// loadZoneIDsFromDB 从 zone_assignments 表加载指定节点的 Zone ID 列表。
// 返回 nil 表示 DB 中无该节点的配置，调用方应回退到环境变量。
func loadZoneIDsFromDB(nodeID string) []uint {
	if nodeID == "" {
		return nil
	}
	var assignments []models.ZoneAssignment
	database.DB.Where("node_id = ?", nodeID).Find(&assignments)
	if len(assignments) == 0 {
		return nil
	}
	ids := make([]uint, 0, len(assignments))
	for _, a := range assignments {
		ids = append(ids, a.ZoneID)
	}
	return ids
}

// reloadZoneConfig 从 DB 重新加载当前节点的车间分配并热更新 ZoneCfg。
// DB 无配置时回退到环境变量初始值。返回更新后的 Zone ID 列表。
func (s *Server) reloadZoneConfig() []uint {
	ids := loadZoneIDsFromDB(s.NodeID)
	if ids == nil {
		ids = s.EnvZoneIDs
		log.Printf("[web] 回退到环境变量 ZoneIDs=%v", ids)
	} else {
		log.Printf("[web] 从 DB 加载 ZoneIDs=%v", ids)
	}
	s.ZoneCfg.Update(ids)
	return ids
}

func (s *Server) Start(port int) {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/hls.min.js", s.handleHlsJS)
	mux.HandleFunc("/api/segments", s.handleSegments)
	mux.HandleFunc("/api/macs", s.handleMACs)
	mux.HandleFunc("/api/streams", s.handleStreams)
	mux.HandleFunc("/api/zone-config", s.handleZoneConfig)
	mux.HandleFunc("/api/zone-assignments", s.handleZoneAssignments)
	mux.HandleFunc("/api/node-settings", s.handleNodeSettings)
	mux.HandleFunc("/api/stats", s.handleStats)
	mux.HandleFunc("/api/health", s.handleHealth)
	mux.HandleFunc("/api/clients/register", s.handleClientRegister)
	mux.HandleFunc("/api/streams/publish-config", s.handlePublishConfig)
	mux.HandleFunc("/api/srs/on-publish", s.handleSRSPublish)
	mux.HandleFunc("/api/srs/on-unpublish", s.handleSRSLifecycle)
	mux.HandleFunc("/api/srs/on-play", s.handleSRSLifecycle)
	mux.HandleFunc("/api/srs/on-stop", s.handleSRSLifecycle)
	mux.HandleFunc("/segments/", s.handleVideo)

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
		Hostname string
		Name     string
		ZoneID   *uint
	}
	compMap := map[string]compRow{}
	if len(macSet) > 0 {
		macs := make([]string, 0, len(macSet))
		for m := range macSet {
			macs = append(macs, m)
		}
		var comps []compRow
		database.DB.Table("computers").
			Select("computers.mac, computers.ip, computers.hostname, users.name, users.zone_id").
			Joins("LEFT JOIN users ON users.id = computers.user_id").
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
			row.Hostname = c.Hostname
			row.UserName = c.Name
			// 指定 Zone 模式下，过滤掉不属于本节点的流
			if !s.isAllZones() {
				if c.ZoneID == nil || !s.zoneAllowed(*c.ZoneID) {
					continue
				}
			}
		} else if !s.isAllZones() {
			// 未注册设备在指定 Zone 模式下不显示
			continue
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

// countFilteredStreams 返回按 Zone 过滤后的在线流数量。
// 全 Zone 模式时返回所有活跃流；指定 Zone 模式时只统计属于本节点的流。
func (s *Server) countFilteredStreams() int {
	streams := s.fetchSRSStreams()
	if s.isAllZones() {
		return len(streams)
	}
	if len(streams) == 0 {
		return 0
	}
	macs := make([]string, 0, len(streams))
	for _, st := range streams {
		if mac := streamNameToMAC(st.Name); mac != "" {
			macs = append(macs, mac)
		}
	}
	type compRow struct {
		MAC    string
		ZoneID *uint
	}
	var comps []compRow
	database.DB.Table("computers").
		Select("computers.mac, users.zone_id").
		Joins("LEFT JOIN users ON users.id = computers.user_id").
		Where("computers.mac IN ?", macs).
		Scan(&comps)
	zoneMap := map[string]*uint{}
	for _, c := range comps {
		zoneMap[c.MAC] = c.ZoneID
	}
	count := 0
	for _, st := range streams {
		mac := streamNameToMAC(st.Name)
		zoneID, ok := zoneMap[mac]
		if !ok || zoneID == nil {
			continue // 未注册或未绑定车间，指定 Zone 模式下不计
		}
		if s.zoneAllowed(*zoneID) {
			count++
		}
	}
	return count
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
// 录制管理
// ============================================================

func (s *Server) handleZoneConfig(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		s.getZoneConfig(w, r)
	case http.MethodPut:
		s.updateZoneConfig(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) getZoneConfig(w http.ResponseWriter, r *http.Request) {
	if s.isAllZones() {
		// 全 Zone 模式：汇总所有车间的录制状态
		var zones []models.Zone
		database.DB.Find(&zones)
		allEnabled := len(zones) > 0
		for _, z := range zones {
			if !z.RecordEnabled {
				allEnabled = false
				break
			}
		}
		resp := map[string]interface{}{
			"zone_id":        0,
			"zone_name":      "所有车间",
			"record_enabled": allEnabled,
			"retain_days":    s.RetainDays,
			"zones":          zones,
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
		return
	}

	// 多 Zone 模式：查询配置的车间
	var zones []models.Zone
	database.DB.Where("id IN ?", s.ZoneCfg.Get()).Find(&zones)
	allEnabled := len(zones) > 0
	for _, z := range zones {
		if !z.RecordEnabled {
			allEnabled = false
			break
		}
	}
	zoneNames := make([]string, 0, len(zones))
	for _, z := range zones {
		zoneNames = append(zoneNames, z.Name)
	}
	resp := map[string]interface{}{
		"zone_id":        0,
		"zone_name":      strings.Join(zoneNames, " + "),
		"record_enabled": allEnabled,
		"retain_days":    s.RetainDays,
		"zones":          zones,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (s *Server) updateZoneConfig(w http.ResponseWriter, r *http.Request) {
	var req struct {
		RecordEnabled *bool `json:"record_enabled"`
		ZoneID        *uint `json:"zone_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "参数错误", http.StatusBadRequest)
		return
	}

	updates := map[string]interface{}{}
	if req.RecordEnabled != nil {
		updates["record_enabled"] = *req.RecordEnabled
	}
	if len(updates) == 0 {
		http.Error(w, "无更新字段", http.StatusBadRequest)
		return
	}

	// 更新指定 zone_id，或更新本节点配置的所有 zone
	query := database.DB.Model(&models.Zone{})
	if req.ZoneID != nil {
		query = query.Where("id = ?", *req.ZoneID)
	} else if !s.isAllZones() {
		query = query.Where("id IN ?", s.ZoneCfg.Get())
	} else {
		// 全 Zone 模式：更新所有车间。GORM v2 不允许无 WHERE 的批量更新，加 1=1 绕过。
		query = query.Where("1 = 1")
	}
	if err := query.Updates(updates).Error; err != nil {
		http.Error(w, fmt.Sprintf("更新失败: %v", err), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"ok": true})
}

// ============================================================
// 车间分配管理（后台维护 RECORDING_NODE_ZONE_ID）
// ============================================================

func (s *Server) handleZoneAssignments(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		s.getZoneAssignments(w, r)
	case http.MethodPost:
		s.addZoneAssignment(w, r)
	case http.MethodDelete:
		s.removeZoneAssignment(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// getZoneAssignments 返回当前节点的车间分配详情、所有可用车间列表、节点信息。
func (s *Server) getZoneAssignments(w http.ResponseWriter, r *http.Request) {
	// 所有车间（带地区信息）。Preload 可能因子账号权限不足而失败，
	// 失败时回退到无 Preload 查询，确保至少能返回车间列表。
	var allZones []models.Zone
	if err := database.DB.Preload("Area.Region").Order("id ASC").Find(&allZones).Error; err != nil {
		log.Printf("[web] Preload Area.Region 失败，回退到无关联查询: %v", err)
		allZones = nil
		database.DB.Order("id ASC").Find(&allZones)
	}

	// 当前节点已分配的车间
	var assigned []models.ZoneAssignment
	if err := database.DB.Preload("Zone.Area.Region").Where("node_id = ?", s.NodeID).Order("zone_id ASC").Find(&assigned).Error; err != nil {
		log.Printf("[web] Preload Zone.Area.Region 失败，回退到无关联查询: %v", err)
		assigned = nil
		database.DB.Where("node_id = ?", s.NodeID).Order("zone_id ASC").Find(&assigned)
	}

	assignedIDs := make([]uint, 0, len(assigned))
	for _, a := range assigned {
		assignedIDs = append(assignedIDs, a.ZoneID)
	}

	resp := map[string]interface{}{
		"node_id":           s.NodeID,
		"node_name":         s.NodeName,
		"assigned_zone_ids": assignedIDs,
		"assigned":          assigned,
		"all_zones":         allZones,
		"current_zone_ids":  s.ZoneCfg.Get(),
		"is_all_zones":      s.isAllZones(),
		"using_db_config":   len(assigned) > 0,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// addZoneAssignment 将指定车间分配到当前节点，并热更新录制配置。
func (s *Server) addZoneAssignment(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ZoneID uint `json:"zone_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ZoneID == 0 {
		http.Error(w, "参数错误：zone_id 必填", http.StatusBadRequest)
		return
	}

	// 校验车间存在
	var zone models.Zone
	if err := database.DB.First(&zone, req.ZoneID).Error; err != nil {
		http.Error(w, "车间不存在", http.StatusBadRequest)
		return
	}

	// 写入分配记录（若已存在则忽略）
	assignment := models.ZoneAssignment{
		NodeID:   s.NodeID,
		NodeName: s.NodeName,
		ZoneID:   req.ZoneID,
	}
	result := database.DB.Where("node_id = ? AND zone_id = ?", s.NodeID, req.ZoneID).
		FirstOrCreate(&assignment)
	if result.Error != nil {
		http.Error(w, "保存失败: "+result.Error.Error(), http.StatusInternalServerError)
		return
	}

	// 若 NodeName 为空或变更过，顺便更新
	if s.NodeName != "" && assignment.NodeName != s.NodeName {
		database.DB.Model(&assignment).Update("node_name", s.NodeName)
	}

	// 热更新
	newIDs := s.reloadZoneConfig()
	log.Printf("[web] 车间 %s(%d) 已分配到节点 %s，当前 ZoneIDs=%v", zone.Name, req.ZoneID, s.NodeID, newIDs)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"ok": true, "zone_ids": newIDs})
}

// removeZoneAssignment 从当前节点移除指定车间，并热更新录制配置。
func (s *Server) removeZoneAssignment(w http.ResponseWriter, r *http.Request) {
	zoneIDStr := r.URL.Query().Get("zone_id")
	zoneID, err := strconv.ParseUint(zoneIDStr, 10, 64)
	if err != nil || zoneID == 0 {
		http.Error(w, "参数错误：zone_id 必填", http.StatusBadRequest)
		return
	}

	result := database.DB.Where("node_id = ? AND zone_id = ?", s.NodeID, zoneID).
		Delete(&models.ZoneAssignment{})
	if result.Error != nil {
		http.Error(w, "删除失败: "+result.Error.Error(), http.StatusInternalServerError)
		return
	}

	// 热更新
	newIDs := s.reloadZoneConfig()
	log.Printf("[web] 车间 %d 已从节点 %s 移除，当前 ZoneIDs=%v", zoneID, s.NodeID, newIDs)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"ok": true, "zone_ids": newIDs})
}

// ============================================================
// 节点设置（录像保留天数等，可后台修改）
// ============================================================

func (s *Server) handleNodeSettings(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		s.getNodeSettings(w, r)
	case http.MethodPut:
		s.updateNodeSettings(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) getNodeSettings(w http.ResponseWriter, r *http.Request) {
	var setting models.NodeSetting
	hasDBConfig := false
	if err := database.DB.Where("node_id = ?", s.NodeID).First(&setting).Error; err == nil {
		hasDBConfig = true
	}
	resp := map[string]interface{}{
		"node_id":         s.NodeID,
		"retain_days":     s.RetainDays,
		"env_retain_days": s.EnvRetainDays,
		"using_db_config": hasDBConfig,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (s *Server) updateNodeSettings(w http.ResponseWriter, r *http.Request) {
	var req struct {
		RetainDays *int `json:"retain_days"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "参数错误", http.StatusBadRequest)
		return
	}
	if req.RetainDays == nil || *req.RetainDays <= 0 {
		http.Error(w, "retain_days 必须大于 0", http.StatusBadRequest)
		return
	}

	// Upsert：有则更新，无则创建
	var setting models.NodeSetting
	if err := database.DB.Where("node_id = ?", s.NodeID).First(&setting).Error; err != nil {
		setting = models.NodeSetting{NodeID: s.NodeID, RetainDays: *req.RetainDays}
		if err := database.DB.Create(&setting).Error; err != nil {
			http.Error(w, fmt.Sprintf("保存失败: %v", err), http.StatusInternalServerError)
			return
		}
	} else {
		if err := database.DB.Model(&setting).Update("retain_days", *req.RetainDays).Error; err != nil {
			http.Error(w, fmt.Sprintf("保存失败: %v", err), http.StatusInternalServerError)
			return
		}
	}

	// 热更新内存值
	s.RetainDays = *req.RetainDays
	if s.UpdateRetainDays != nil {
		s.UpdateRetainDays(*req.RetainDays)
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
	zoneCond, zoneArgs := s.zoneFilter()
	statsQuery := database.DB.Model(&models.RecordingSegment{}).Where("storage = ?", "local")
	if zoneCond != "" {
		statsQuery = statsQuery.Where(zoneCond, zoneArgs...)
	}
	statsQuery.Count(&segCount)

	statsQuery2 := database.DB.Model(&models.RecordingSegment{}).Where("storage = ?", "local")
	if zoneCond != "" {
		statsQuery2 = statsQuery2.Where(zoneCond, zoneArgs...)
	}
	statsQuery2.Select("COALESCE(SUM(file_size), 0)").Scan(&totalSize)

	// 磁盘使用率
	diskTotal, diskFree := diskUsage(".")
	diskUsed := diskTotal - diskFree
	diskPercent := 0.0
	if diskTotal > 0 {
		diskPercent = float64(diskUsed) / float64(diskTotal) * 100
	}

	// 在线流数量（按 Zone 过滤）
	onlineStreams := s.countFilteredStreams()

	resp := map[string]interface{}{
		"seg_count":      segCount,
		"total_size":     totalSize,
		"online_streams": onlineStreams,
		"disk_total":     diskTotal,
		"disk_free":      diskFree,
		"disk_used":      diskUsed,
		"disk_percent":   diskPercent,
		"srs_http_host":  s.SRSHttpHost,
		"node_zone_ids":  s.ZoneCfg.Get(),
		"node_id":        s.NodeID,
		"node_name":      s.NodeName,
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
	zoneCond, zoneArgs := s.zoneFilter()
	if zoneCond != "" {
		query = query.Where(zoneCond, zoneArgs...)
	}

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
	zoneCond, zoneArgs := s.zoneFilter()
	if zoneCond != "" {
		macQuery = macQuery.Where(zoneCond, zoneArgs...)
	}
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
	zoneCond, zoneArgs := s.zoneFilter()
	if zoneCond != "" {
		segQuery = segQuery.Where(zoneCond, zoneArgs...)
	}
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
