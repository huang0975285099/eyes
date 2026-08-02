package recording

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log"
	"mime/multipart"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"recording-service/database"
	"recording-service/models"
	"recording-service/nodeconfig"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	defaultSegmentDuration = 600
	defaultCheckInterval   = 30
	defaultRetainDays      = 7
	defaultFrameRetainDays = 30
	extractConcurrency     = 16

	// SRS 的 /api/v1/streams 默认只返回前 ~10 条（HTTP API 内置分页上限）。
	// 不带 count 时会漏掉绝大多数在推的流，导致只录到一小撮设备。
	// 显式带足够大的 count 取全量；SRS 会返回 min(实际, count)。
	srsStreamsQueryCount = 100000
)

type Config struct {
	SRSApiBase      string
	RTMPHost        string
	OutputDir       string
	SegmentDuration int
	CheckInterval   int
	RetainDays      int
	FrameRetainDays int
	FFmpegPath      string
	NodeZoneIDs     []uint
	CenterURL       string
	CenterKey       string
}

type srsStream struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	App     string `json:"app"`
	Publish struct {
		Active bool `json:"active"`
	} `json:"publish"`
}

type srsStreamsResp struct {
	Code    int         `json:"code"`
	Streams []srsStream `json:"streams"`
}

type Recorder struct {
	StreamName string
	Cmd        *exec.Cmd
	Cancel     context.CancelFunc
	StartedAt  time.Time
	OutputDir  string
}

type RecorderManager struct {
	mu             sync.Mutex
	wg             sync.WaitGroup
	active         map[string]*Recorder
	cfg            Config
	zoneCfg        *nodeconfig.ZoneConfig // 与 web Server 共享的 Zone 配置，支持热更新
	client         *http.Client           // SRS API 查询，短超时
	pushClient     *http.Client           // 帧推送到中心节点，长超时 + 连接池
	extractSem     chan struct{}
	corruptedPaths sync.Map // 已确认损坏的文件路径集合，避免每轮扫盘重复 ffprobe
}

func NewRecorderManager(cfg Config) *RecorderManager {
	if cfg.SegmentDuration <= 0 {
		cfg.SegmentDuration = defaultSegmentDuration
	}
	if cfg.CheckInterval <= 0 {
		cfg.CheckInterval = defaultCheckInterval
	}
	if cfg.RetainDays <= 0 {
		cfg.RetainDays = defaultRetainDays
	}
	if cfg.FrameRetainDays <= 0 {
		cfg.FrameRetainDays = defaultFrameRetainDays
	}
	if cfg.FFmpegPath == "" {
		cfg.FFmpegPath = "ffmpeg"
	}
	if cfg.OutputDir == "" {
		cfg.OutputDir = "/var/recordings"
	}
	return &RecorderManager{
		active:  make(map[string]*Recorder),
		cfg:     cfg,
		zoneCfg: nodeconfig.New(cfg.NodeZoneIDs),
		client:  &http.Client{Timeout: 10 * time.Second},
		pushClient: &http.Client{
			Timeout: 60 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns:        10,
				MaxIdleConnsPerHost: 6,
				IdleConnTimeout:     90 * time.Second,
			},
		},
		extractSem: make(chan struct{}, extractConcurrency),
	}
}

// ZoneConfig 返回共享的 Zone 配置实例，供 web Server 引用以实现热更新。
func (m *RecorderManager) ZoneConfig() *nodeconfig.ZoneConfig {
	return m.zoneCfg
}

// UpdateZoneIDs 运行时更新录制的 Zone ID 列表（后台修改车间分配后调用）。
func (m *RecorderManager) UpdateZoneIDs(ids []uint) {
	m.zoneCfg.Update(ids)
	if m.zoneCfg.IsAllZones() {
		log.Printf("[recording] ZoneIDs 已更新：录制所有车间")
	} else {
		log.Printf("[recording] ZoneIDs 已更新为 %v", ids)
	}
}

func (m *RecorderManager) Run(ctx context.Context) {
	if err := os.MkdirAll(m.cfg.OutputDir, 0755); err != nil {
		log.Printf("[recording] 创建输出目录失败 %s: %v", m.cfg.OutputDir, err)
		return
	}

	m.cleanupZeroByteSegments()
	m.syncRecordings()
	m.indexExistingFiles()

	ticker := time.NewTicker(time.Duration(m.cfg.CheckInterval) * time.Second)
	defer ticker.Stop()

	cleanupTicker := time.NewTicker(1 * time.Hour)
	defer cleanupTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			m.stopAll()
			return
		case <-ticker.C:
			m.syncRecordings()
			m.indexExistingFiles()
		case <-cleanupTicker.C:
			m.cleanupExpired()
		}
	}
}

func (m *RecorderManager) Wait() {
	m.wg.Wait()
}

// UpdateRetainDays 热更新录像保留天数，立即生效无需重启。
func (m *RecorderManager) UpdateRetainDays(days int) {
	if days <= 0 {
		return
	}
	m.mu.Lock()
	m.cfg.RetainDays = days
	m.mu.Unlock()
	log.Printf("[recording] 录像保留天数已更新为 %d 天", days)
}

func (m *RecorderManager) syncRecordings() {
	streams := m.fetchActiveStreams()
	disabledZones := m.loadDisabledZones()
	m.mu.Lock()
	defer m.mu.Unlock()

	activeSet := make(map[string]struct{}, len(streams))
	for _, s := range streams {
		if !m.shouldRecord(s.Name, disabledZones) {
			continue
		}
		activeSet[s.Name] = struct{}{}
		if _, ok := m.active[s.Name]; !ok {
			m.startRecording(s.Name)
		}
	}

	for name, rec := range m.active {
		if _, ok := activeSet[name]; !ok {
			m.stopRecording(name, rec)
		}
	}
}

// loadDisabledZones 返回当前关闭了服务端录制的车间 ID 集合（record_enabled=false）。
func (m *RecorderManager) loadDisabledZones() map[uint]bool {
	var ids []uint
	database.DB.Model(&models.Zone{}).Where("record_enabled = ?", false).Pluck("id", &ids)
	if len(ids) == 0 {
		return nil
	}
	set := make(map[uint]bool, len(ids))
	for _, id := range ids {
		set[id] = true
	}
	return set
}

func (m *RecorderManager) shouldRecord(streamName string, disabledZones map[uint]bool) bool {
	mac := streamNameToMAC(streamName)
	var computer models.Computer
	if err := database.DB.Preload("User").Where("mac = ?", mac).First(&computer).Error; err != nil {
		// 全 Zone 模式时允许未注册设备录制；指定 Zone 时拒绝
		return m.zoneCfg.IsAllZones()
	}
	if computer.UserID == nil || computer.User.ZoneID == nil {
		return m.zoneCfg.IsAllZones()
	}
	zoneID := *computer.User.ZoneID
	// 检查是否在配置的 Zone 范围内
	if !m.zoneCfg.ZoneAllowed(zoneID) {
		return false
	}
	// 车间录制开关：被关闭的车间不启动录制。
	if disabledZones[zoneID] {
		return false
	}
	return true
}

// lookupZoneID 返回设备所属的 Zone ID（始终查数据库获取实际值）。
func (m *RecorderManager) lookupZoneID(streamName string) uint {
	mac := streamNameToMAC(streamName)
	var computer models.Computer
	if err := database.DB.Preload("User").Where("mac = ?", mac).First(&computer).Error; err != nil {
		return 0
	}
	if computer.UserID == nil || computer.User.ZoneID == nil {
		return 0
	}
	return *computer.User.ZoneID
}

func (m *RecorderManager) fetchActiveStreams() []srsStream {
	url := strings.TrimRight(m.cfg.SRSApiBase, "/") + "/api/v1/streams/?count=" + strconv.Itoa(srsStreamsQueryCount)
	resp, err := m.client.Get(url)
	if err != nil {
		log.Printf("[recording] 查询 SRS 流列表失败: %v", err)
		return nil
	}
	defer resp.Body.Close()

	var body srsStreamsResp
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		log.Printf("[recording] 解析 SRS 流列表失败: %v", err)
		return nil
	}
	if body.Code != 0 {
		log.Printf("[recording] SRS 返回非零 code: %d", body.Code)
		return nil
	}

	seen := make(map[string]struct{}, len(body.Streams))
	result := body.Streams[:0]
	for _, s := range body.Streams {
		s.Name = strings.TrimSuffix(s.Name, ".flv")
		if s.Name == "" || !s.Publish.Active {
			continue
		}
		if _, dup := seen[s.Name]; dup {
			continue
		}
		seen[s.Name] = struct{}{}
		result = append(result, s)
	}
	return result
}

// buildRTMPURL 根据 RTMPHost 配置构建 RTMP URL。
// RTMPHost 支持两种格式："host" 或 "host:port"，未带端口时默认 1935。
func buildRTMPURL(rtmpHost, streamName string) string {
	host, port, err := net.SplitHostPort(rtmpHost)
	if err != nil {
		// 不含端口，使用默认 1935
		return fmt.Sprintf("rtmp://%s:1935/live/%s", rtmpHost, streamName)
	}
	return fmt.Sprintf("rtmp://%s:%s/live/%s", host, port, streamName)
}

func (m *RecorderManager) startRecording(streamName string) {
	rtmpURL := buildRTMPURL(m.cfg.RTMPHost, streamName)
	dayDir := time.Now().Format("2006-01-02")
	outputDir := filepath.Join(m.cfg.OutputDir, streamName, dayDir)
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		log.Printf("[recording] 创建录像目录失败 %s: %v", outputDir, err)
		return
	}

	ctx, cancel := context.WithCancel(context.Background())

	segmentFilename := filepath.Join(outputDir, "%Y%m%d_%H%M%S.mp4")
	args := []string{
		"-rw_timeout", "15000000",
		"-i", rtmpURL,
		"-c", "copy",
		"-f", "segment",
		"-segment_time", strconv.Itoa(m.cfg.SegmentDuration),
		"-segment_format", "mp4",
		"-segment_format_options", "movflags=frag_keyframe+empty_moov",
		"-reset_timestamps", "1",
		"-strftime", "1",
		"-loglevel", "warning",
		segmentFilename,
	}

	cmd := exec.CommandContext(ctx, m.cfg.FFmpegPath, args...)
	cmd.Stdout = nil
	cmd.Stderr = os.Stderr
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = 30 * time.Second

	if err := cmd.Start(); err != nil {
		cancel()
		log.Printf("[recording] 启动 FFmpeg 录制失败 stream=%s: %v", streamName, err)
		return
	}

	thisRec := &Recorder{
		StreamName: streamName,
		Cmd:        cmd,
		Cancel:     cancel,
		StartedAt:  time.Now(),
		OutputDir:  outputDir,
	}
	m.active[streamName] = thisRec
	log.Printf("[recording] 开始录制 stream=%s pid=%d", streamName, cmd.Process.Pid)

	m.wg.Add(1)
	go func() {
		defer m.wg.Done()
		err := cmd.Wait()
		m.mu.Lock()
		defer m.mu.Unlock()
		if current, ok := m.active[streamName]; ok && current == thisRec {
			delete(m.active, streamName)
		}
		if err != nil && ctx.Err() == nil {
			log.Printf("[recording] FFmpeg 异常退出 stream=%s: %v", streamName, err)
		} else {
			log.Printf("[recording] FFmpeg 退出 stream=%s", streamName)
		}
	}()
}

func (m *RecorderManager) stopRecording(name string, rec *Recorder) {
	log.Printf("[recording] 停止录制 stream=%s", name)
	rec.Cancel()
	delete(m.active, name)
}

func (m *RecorderManager) stopAll() {
	m.mu.Lock()
	for name, rec := range m.active {
		m.stopRecording(name, rec)
	}
	m.mu.Unlock()
	m.wg.Wait()
}

func (m *RecorderManager) indexExistingFiles() {
	root := m.cfg.OutputDir
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return
	}

	entries, err := os.ReadDir(root)
	if err != nil {
		return
	}

	for _, streamEntry := range entries {
		if !streamEntry.IsDir() {
			continue
		}
		streamName := streamEntry.Name()
		if streamName == "_frames" {
			continue
		}
		streamDir := filepath.Join(root, streamName)

		dayEntries, err := os.ReadDir(streamDir)
		if err != nil {
			continue
		}

		for _, dayEntry := range dayEntries {
			if !dayEntry.IsDir() {
				continue
			}
			dayDir := filepath.Join(streamDir, dayEntry.Name())
			m.indexDayDir(streamName, dayDir, dayEntry.Name())
		}
	}
}

func (m *RecorderManager) indexDayDir(streamName, dayDir, dayStr string) {
	entries, err := os.ReadDir(dayDir)
	if err != nil {
		return
	}

	mac := streamNameToMAC(streamName)

	// 先收集本目录所有 mp4 路径，一次性批量查出已入库的，避免逐文件 SELECT count(*)。
	var candidates []string
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasSuffix(strings.ToLower(name), ".mp4") {
			continue
		}
		candidates = append(candidates, filepath.Join(dayDir, name))
	}
	if len(candidates) == 0 {
		return
	}
	indexed := make(map[string]struct{}, len(candidates))
	var existing []string
	database.DB.Model(&models.RecordingSegment{}).
		Where("file_path IN ?", candidates).
		Pluck("file_path", &existing)
	for _, p := range existing {
		indexed[p] = struct{}{}
	}

	for _, filePath := range candidates {
		name := filepath.Base(filePath)
		if _, ok := indexed[filePath]; ok {
			continue // 已入库，跳过
		}

		startedAt, _, _ := parseSegmentTimes(name, dayStr, m.cfg.SegmentDuration)
		if startedAt.IsZero() {
			continue
		}

		var fi fs.FileInfo
		fi, err = os.Stat(filePath)
		if err != nil {
			continue
		}
		if time.Since(fi.ModTime()) < 30*time.Second {
			continue
		}
		if fi.Size() == 0 {
			os.Remove(filePath)
			log.Printf("[recording] 跳过并删除 0 字节文件 %s", filePath)
			continue
		}

		// 已确认损坏的文件跳过 ffprobe，避免每轮 30s 重复启动进程消耗 CPU
		if _, bad := m.corruptedPaths.Load(filePath); bad {
			continue
		}

		actualDuration, corrupted := m.probeDuration(filePath)
		if corrupted {
			m.corruptedPaths.Store(filePath, struct{}{})
			log.Printf("[recording] 文件损坏（无 moov 原子），跳过索引: %s", filePath)
			continue
		}
		if actualDuration <= 0 {
			actualDuration = float64(m.cfg.SegmentDuration)
		}
		endedAt := startedAt.Add(time.Duration(actualDuration * float64(time.Second)))

		seg := models.RecordingSegment{
			StreamName: streamName,
			MAC:        mac,
			FilePath:   filePath,
			FileSize:   fi.Size(),
			StartedAt:  startedAt,
			EndedAt:    endedAt,
			Duration:   actualDuration,
			Storage:    "local",
			NodeZoneID: m.lookupZoneID(streamName),
		}
		if err := database.DB.Create(&seg).Error; err != nil {
			log.Printf("[recording] 写入索引失败 %s: %v", filePath, err)
		} else {
			log.Printf("[recording] 索引新片段 %s (%.0fs)", filePath, actualDuration)
			go m.extractFrames(streamName, mac, seg.ID, filePath, startedAt, m.cfg.SegmentDuration)
		}
	}
}

func (m *RecorderManager) extractFrames(streamName, mac string, segmentID uint, segmentPath string, startedAt time.Time, segDuration int) {
	m.extractSem <- struct{}{}
	defer func() { <-m.extractSem }()

	actualDuration, corrupted := m.probeDuration(segmentPath)
	if corrupted {
		return
	}
	if actualDuration <= 0 {
		actualDuration = float64(segDuration)
	}
	if actualDuration < 30 {
		return
	}

	effectiveDur := int(actualDuration)
	frameCount := 2
	if effectiveDur >= 600 {
		frameCount = effectiveDur / 300
	}
	interval := effectiveDur / frameCount

	framesDir := filepath.Join(m.cfg.OutputDir, "_frames", streamName)
	if err := os.MkdirAll(framesDir, 0755); err != nil {
		log.Printf("[recording] 创建抽帧目录失败 %s: %v", framesDir, err)
		return
	}

	for i := 0; i < frameCount; i++ {
		offset := interval*i + interval/2
		capturedAt := startedAt.Add(time.Duration(offset) * time.Second)

		frameName := fmt.Sprintf("%s_f%d.jpg",
			strings.TrimSuffix(filepath.Base(segmentPath), filepath.Ext(segmentPath)),
			i+1,
		)
		framePath := filepath.Join(framesDir, frameName)

		// 以 (segment_id, frame_index) 去重，兼容本地模式和推送模式
		var exist int64
		database.DB.Model(&models.RecordingFrame{}).
			Where("segment_id = ? AND frame_index = ?", segmentID, i+1).
			Count(&exist)
		if exist > 0 {
			continue
		}

		// 文件已存在（上次推送/保存成功但 DB 写入失败时重试）
		if _, err := os.Stat(framePath); err == nil {
			if m.cfg.CenterURL != "" {
				if !m.pushFrameToCenter(streamName, mac, segmentID, framePath, capturedAt, i+1) {
					// 推送失败且不会重试（extractFrames 对同一片段只调用一次），删除文件避免永久堆积
					os.Remove(framePath)
				}
			} else {
				m.indexFrame(streamName, mac, segmentID, framePath, capturedAt, i+1)
			}
			continue
		}

		args := []string{
			"-ss", fmt.Sprintf("%d", offset),
			"-i", segmentPath,
			"-frames:v", "1",
			"-q:v", "2",
			"-y",
			framePath,
		}
		cmd := exec.Command(m.cfg.FFmpegPath, args...)
		cmd.Stdout = nil
		cmd.Stderr = nil
		if err := cmd.Run(); err != nil {
			log.Printf("[recording] 抽帧失败 %s offset=%ds: %v", segmentPath, offset, err)
			continue
		}

		if m.cfg.CenterURL != "" {
			if !m.pushFrameToCenter(streamName, mac, segmentID, framePath, capturedAt, i+1) {
				os.Remove(framePath)
			}
		} else {
			m.indexFrame(streamName, mac, segmentID, framePath, capturedAt, i+1)
		}
	}
}

func (m *RecorderManager) indexFrame(streamName, mac string, segmentID uint, framePath string, capturedAt time.Time, frameIndex int) {
	fi, err := os.Stat(framePath)
	if err != nil {
		return
	}
	frame := models.RecordingFrame{
		StreamName: streamName,
		MAC:        mac,
		SegmentID:  segmentID,
		FilePath:   framePath,
		FileSize:   fi.Size(),
		CapturedAt: capturedAt,
		FrameIndex: frameIndex,
		NodeZoneID: m.lookupZoneID(streamName),
	}
	if err := database.DB.Create(&frame).Error; err != nil {
		log.Printf("[recording] 写入帧索引失败 %s: %v", framePath, err)
	}
}

// pushFrameToCenter 把本节点抽取的帧图片推送到中心节点（A节点）并在成功后删除本地临时文件。
// 返回 true 表示推送成功。
func (m *RecorderManager) pushFrameToCenter(streamName, mac string, segmentID uint, framePath string, capturedAt time.Time, frameIndex int) bool {
	f, err := os.Open(framePath)
	if err != nil {
		log.Printf("[recording] 打开帧文件失败 %s: %v", framePath, err)
		return false
	}
	defer f.Close()

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	part, err := writer.CreateFormFile("file", filepath.Base(framePath))
	if err != nil {
		return false
	}
	if _, err := io.Copy(part, f); err != nil {
		return false
	}
	writer.WriteField("stream_name", streamName)
	writer.WriteField("mac", mac)
	writer.WriteField("segment_id", strconv.FormatUint(uint64(segmentID), 10))
	writer.WriteField("captured_at", capturedAt.Format(time.RFC3339))
	writer.WriteField("frame_index", strconv.Itoa(frameIndex))
	writer.WriteField("node_zone_id", strconv.FormatUint(uint64(m.lookupZoneID(streamName)), 10))
	writer.Close()

	url := strings.TrimRight(m.cfg.CenterURL, "/") + "/api/recording-frames/upload"
	req, err := http.NewRequest("POST", url, body)
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("X-Proxy-Key", m.cfg.CenterKey)

	resp, err := m.pushClient.Do(req)
	if err != nil {
		log.Printf("[recording] 推送帧到中心节点失败 %s: %v", filepath.Base(framePath), err)
		return false
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		log.Printf("[recording] 推送帧失败 status=%d %s", resp.StatusCode, filepath.Base(framePath))
		return false
	}

	os.Remove(framePath)
	log.Printf("[recording] 帧已推送到中心节点并删除本地文件 %s", filepath.Base(framePath))
	return true
}

func (m *RecorderManager) cleanupExpired() {
	m.mu.Lock()
	retainDays := m.cfg.RetainDays
	m.mu.Unlock()
	mainCutoff := time.Now().AddDate(0, 0, -retainDays)
	zCond, zArgs := m.zoneCfg.ZoneCond()

	// 只清理本节点产生的 local 片段（node_zone_id 匹配）
	segWhere := "storage = ? AND started_at < ?"
	segArgs := []interface{}{"local", mainCutoff}
	if zCond != "" {
		segWhere += " AND " + zCond
		segArgs = append(segArgs, zArgs...)
	}
	var localSegments []models.RecordingSegment
	database.DB.Unscoped().Where(segWhere, segArgs...).Find(&localSegments)
	for _, seg := range localSegments {
		if seg.FilePath != "" {
			if err := os.Remove(seg.FilePath); err != nil && !os.IsNotExist(err) {
				log.Printf("[recording] 删除过期录像文件失败 %s: %v", seg.FilePath, err)
			}
		}
	}

	// 清理过期的损坏文件：损坏文件不入库，上面基于 DB 的清理覆盖不到，会永久占盘
	m.cleanupCorruptedFiles(mainCutoff)

	// 推送模式下帧文件已在 A 节点，由 A 节点负责生命周期，本节点不清理帧
	if m.cfg.CenterURL == "" {
		frameCutoff := time.Now().AddDate(0, 0, -m.cfg.FrameRetainDays)
		frameWhere := "captured_at < ?"
		frameArgs := []interface{}{frameCutoff}
		if zCond != "" {
			frameWhere += " AND " + zCond
			frameArgs = append(frameArgs, zArgs...)
		}
		var expiredFrames []models.RecordingFrame
		database.DB.Unscoped().Where(frameWhere, frameArgs...).Find(&expiredFrames)
		for _, f := range expiredFrames {
			if f.FilePath != "" {
				if err := os.Remove(f.FilePath); err != nil && !os.IsNotExist(err) {
					log.Printf("[recording] 删除过期帧文件失败 %s: %v", f.FilePath, err)
				}
			}
		}
		if len(expiredFrames) > 0 {
			database.DB.Unscoped().Where(frameWhere, frameArgs...).Delete(&models.RecordingFrame{})
			log.Printf("[recording] 清理过期帧 %d 条", len(expiredFrames))
		}
	}

	result := database.DB.Unscoped().Where(segWhere, segArgs...).Delete(&models.RecordingSegment{})
	if result.RowsAffected > 0 {
		log.Printf("[recording] 清理过期片段 %d 条", result.RowsAffected)
	}

	m.cleanupEmptyDirs()
}

// cleanupCorruptedFiles 删除已确认损坏且超过保留期的孤立文件。
// 损坏文件不入库，cleanupExpired 基于 DB 的查询覆盖不到它们，否则会永久占盘。
// 以文件 mtime（≈录制时间）判断是否超期；同时清除已消失文件的内存标记，
// 使 corruptedPaths 不会无限增长。进程重启后由 indexExistingFiles 重新探测填充。
func (m *RecorderManager) cleanupCorruptedFiles(cutoff time.Time) {
	var removed int
	m.corruptedPaths.Range(func(key, _ any) bool {
		fp, ok := key.(string)
		if !ok || fp == "" {
			return true
		}
		fi, err := os.Stat(fp)
		if err != nil {
			if os.IsNotExist(err) {
				m.corruptedPaths.Delete(fp) // 文件已被手动删除等，清除标记
			}
			return true
		}
		if fi.ModTime().Before(cutoff) {
			if err := os.Remove(fp); err != nil && !os.IsNotExist(err) {
				log.Printf("[recording] 删除过期损坏文件失败 %s: %v", fp, err)
				return true
			}
			m.corruptedPaths.Delete(fp)
			removed++
		}
		return true
	})
	if removed > 0 {
		log.Printf("[recording] 清理过期损坏文件 %d 个", removed)
	}
}

func (m *RecorderManager) cleanupZeroByteSegments() {
	zCond, zArgs := m.zoneCfg.ZoneCond()
	where := "file_size = 0"
	args := []interface{}{}
	if zCond != "" {
		where += " AND " + zCond
		args = append(args, zArgs...)
	}
	var zeroSegs []models.RecordingSegment
	database.DB.Unscoped().Where(where, args...).Find(&zeroSegs)
	for _, seg := range zeroSegs {
		if seg.FilePath != "" {
			if err := os.Remove(seg.FilePath); err != nil && !os.IsNotExist(err) {
				log.Printf("[recording] 删除 0 字节文件失败 %s: %v", seg.FilePath, err)
			}
		}
	}
	if len(zeroSegs) > 0 {
		database.DB.Unscoped().Where(where, args...).Delete(&models.RecordingSegment{})
		log.Printf("[recording] 清理 0 字节片段 %d 条", len(zeroSegs))
	}
}

func (m *RecorderManager) probeDuration(filePath string) (float64, bool) {
	ffmpegBase := filepath.Base(m.cfg.FFmpegPath)
	probeBase := strings.Replace(ffmpegBase, "ffmpeg", "ffprobe", 1)
	var probePath string
	if probeBase == ffmpegBase {
		probePath = "ffprobe"
	} else if filepath.Dir(m.cfg.FFmpegPath) == "." {
		probePath = probeBase
	} else {
		probePath = filepath.Join(filepath.Dir(m.cfg.FFmpegPath), probeBase)
	}

	cmd := exec.Command(probePath,
		"-v", "error",
		"-show_entries", "format=duration",
		"-of", "default=noprint_wrappers=1:nokey=1",
		filePath,
	)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		errMsg := stderr.String()
		if strings.Contains(errMsg, "moov atom not found") ||
			strings.Contains(errMsg, "Invalid data found") ||
			strings.Contains(errMsg, "Format mp4 detected only with low score") {
			return 0, true
		}
		return 0, false
	}
	d, err := strconv.ParseFloat(strings.TrimSpace(string(out)), 64)
	if err != nil || d <= 0 {
		return 0, false
	}
	return d, false
}

func (m *RecorderManager) cleanupEmptyDirs() {
	root := m.cfg.OutputDir
	entries, err := os.ReadDir(root)
	if err != nil {
		return
	}
	for _, streamEntry := range entries {
		if !streamEntry.IsDir() {
			continue
		}
		if streamEntry.Name() == "_frames" {
			continue
		}
		streamDir := filepath.Join(root, streamEntry.Name())
		dayEntries, err := os.ReadDir(streamDir)
		if err != nil {
			continue
		}
		for _, dayEntry := range dayEntries {
			if !dayEntry.IsDir() {
				continue
			}
			dayPath := filepath.Join(streamDir, dayEntry.Name())
			files, _ := os.ReadDir(dayPath)
			if len(files) == 0 {
				os.Remove(dayPath)
			}
		}
		dayEntries2, _ := os.ReadDir(streamDir)
		if len(dayEntries2) == 0 {
			os.Remove(streamDir)
		}
	}
}

func streamNameToMAC(s string) string {
	s = strings.ToLower(s)
	if len(s) != 12 {
		return s
	}
	return fmt.Sprintf("%s:%s:%s:%s:%s:%s",
		s[0:2], s[2:4], s[4:6], s[6:8], s[8:10], s[10:12])
}

func parseSegmentTimes(filename, dayStr string, segDuration int) (startedAt, endedAt time.Time, duration float64) {
	base := strings.TrimSuffix(filename, filepath.Ext(filename))

	if len(base) == 15 && base[8] == '_' {
		ts := base[:4] + "-" + base[4:6] + "-" + base[6:8] + "T" +
			base[9:11] + ":" + base[11:13] + ":" + base[13:15] + "+08:00"
		if t, err := time.Parse(time.RFC3339, ts); err == nil {
			startedAt = t
			duration = float64(segDuration)
			endedAt = t.Add(time.Duration(duration) * time.Second)
			return
		}
	}

	if len(base) >= 6 {
		ts := dayStr + "T" + base[0:2] + ":" + base[2:4] + ":" + base[4:6] + "+08:00"
		if t, err := time.Parse(time.RFC3339, ts); err == nil {
			startedAt = t
			duration = float64(segDuration)
			endedAt = t.Add(time.Duration(duration) * time.Second)
			return
		}
	}

	return
}
