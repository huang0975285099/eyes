package recording

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"media-service/analysis"
	"media-service/database"
	"media-service/models"
	"media-service/streamsource"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	defaultSegmentDuration  = 600
	defaultCheckInterval    = 30
	defaultRetainHours      = 48
	defaultFrameRetainDays  = 30
	defaultEventRetainHours = 30 * 24
	cleanupInterval         = 10 * time.Minute
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
	RetainHours     int
	// FrameRetainDays controls lifecycle cleanup for AIService-produced frame
	// artifacts. Frame extraction itself does not run in MediaService.
	FrameRetainDays int
	// EventRetainHours is the fallback for realtime rules without a valid
	// retain_hours value in config_json.
	EventRetainHours int
	FFmpegPath       string
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
	syncMu         sync.Mutex // prevent an older poll from overriding a freshly saved rule
	wg             sync.WaitGroup
	active         map[string]*Recorder
	cfg            Config
	client         *http.Client // SRS API 查询，短超时
	corruptedPaths sync.Map     // 已确认损坏的文件路径集合，避免每轮扫盘重复 ffprobe
}

func NewRecorderManager(cfg Config) *RecorderManager {
	if cfg.SegmentDuration <= 0 {
		cfg.SegmentDuration = defaultSegmentDuration
	}
	if cfg.CheckInterval <= 0 {
		cfg.CheckInterval = defaultCheckInterval
	}
	if cfg.RetainHours <= 0 {
		cfg.RetainHours = defaultRetainHours
	}
	if cfg.FrameRetainDays <= 0 {
		cfg.FrameRetainDays = defaultFrameRetainDays
	}
	if cfg.EventRetainHours <= 0 {
		cfg.EventRetainHours = defaultEventRetainHours
	}
	if cfg.FFmpegPath == "" {
		cfg.FFmpegPath = "ffmpeg"
	}
	if cfg.OutputDir == "" {
		cfg.OutputDir = "/var/recordings"
	}
	return &RecorderManager{
		active: make(map[string]*Recorder),
		cfg:    cfg,
		client: &http.Client{Timeout: 10 * time.Second},
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
	m.cleanupExpired()

	ticker := time.NewTicker(time.Duration(m.cfg.CheckInterval) * time.Second)
	defer ticker.Stop()

	cleanupTicker := time.NewTicker(cleanupInterval)
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

// RefreshRules immediately applies per-source recording changes made in the
// customer portal instead of waiting for the regular SRS polling interval.
func (m *RecorderManager) RefreshRules() { go m.syncRecordings() }

func (m *RecorderManager) syncRecordings() {
	m.syncMu.Lock()
	defer m.syncMu.Unlock()
	streams, ok := m.fetchActiveStreams()
	if !ok {
		return
	}
	var enabledNames []string
	if err := database.DB.Table("video_sources").
		Select("video_sources.stream_name").
		Joins("JOIN video_recording_rules ON video_recording_rules.video_source_id = video_sources.id").
		Where("video_sources.enabled = ? AND video_recording_rules.enabled = ?", true, true).
		Pluck("video_sources.stream_name", &enabledNames).Error; err != nil {
		log.Printf("[recording] 查询按视频源录像规则失败: %v", err)
		return
	}
	enabledSet := make(map[string]struct{}, len(enabledNames))
	for _, name := range enabledNames {
		enabledSet[name] = struct{}{}
	}
	m.mu.Lock()
	defer m.mu.Unlock()

	activeSet := make(map[string]struct{}, len(streams))
	for _, s := range streams {
		if _, enabled := enabledSet[s.Name]; !enabled {
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

func (m *RecorderManager) fetchActiveStreams() ([]srsStream, bool) {
	url := strings.TrimRight(m.cfg.SRSApiBase, "/") + "/api/v1/streams/?count=" + strconv.Itoa(srsStreamsQueryCount)
	resp, err := m.client.Get(url)
	if err != nil {
		log.Printf("[recording] 查询 SRS 流列表失败: %v", err)
		return nil, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.Printf("[recording] SRS 流列表返回 HTTP %d", resp.StatusCode)
		return nil, false
	}

	var body srsStreamsResp
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		log.Printf("[recording] 解析 SRS 流列表失败: %v", err)
		return nil, false
	}
	if body.Code != 0 {
		log.Printf("[recording] SRS 返回非零 code: %d", body.Code)
		return nil, false
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
	return result, true
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
		if streamName == "_frames" || streamName == "_events" {
			continue
		}
		streamDir := filepath.Join(root, streamName)
		customerID, mac, sourceType, sourceID := lookupVideoSource(streamName)

		dayEntries, err := os.ReadDir(streamDir)
		if err != nil {
			continue
		}

		for _, dayEntry := range dayEntries {
			if !dayEntry.IsDir() {
				continue
			}
			dayDir := filepath.Join(streamDir, dayEntry.Name())
			m.indexDayDir(customerID, streamName, mac, sourceType, sourceID, dayDir, dayEntry.Name())
		}
	}
}

func (m *RecorderManager) indexDayDir(customerID uint, streamName, mac, sourceType, sourceID, dayDir, dayStr string) {
	entries, err := os.ReadDir(dayDir)
	if err != nil {
		return
	}

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
			CustomerID: customerID,
			StreamName: streamName,
			MAC:        mac,
			SourceType: sourceType,
			SourceID:   sourceID,
			FilePath:   filePath,
			FileSize:   fi.Size(),
			StartedAt:  startedAt,
			EndedAt:    endedAt,
			Duration:   actualDuration,
			Storage:    "local",
		}
		if err := database.DB.Create(&seg).Error; err != nil {
			log.Printf("[recording] 写入索引失败 %s: %v", filePath, err)
		} else {
			log.Printf("[recording] 索引新片段 %s (%.0fs)", filePath, actualDuration)
		}
	}
}

func (m *RecorderManager) cleanupExpired() {
	now := time.Now()
	type retainRule struct {
		StreamName  string `gorm:"column:stream_name"`
		RetainHours int    `gorm:"column:retain_hours"`
	}
	var rules []retainRule
	database.DB.Table("video_sources").
		Select("video_sources.stream_name, video_recording_rules.retain_hours").
		Joins("JOIN video_recording_rules ON video_recording_rules.video_source_id = video_sources.id").
		Find(&rules)
	retention := make(map[string]int, len(rules))
	for _, rule := range rules {
		if rule.RetainHours <= 0 {
			rule.RetainHours = m.cfg.RetainHours
		}
		retention[rule.StreamName] = rule.RetainHours
	}

	// Every source has an independent retention period. Query candidates older
	// than one hour, then apply the source-specific cutoff in memory. Retention
	// starts when a segment ends so every recorded frame receives the full
	// configured lifetime.
	var candidates []models.RecordingSegment
	database.DB.Unscoped().Where("storage = ? AND ended_at < ?", "local", now.Add(-time.Hour)).Find(&candidates)
	var localSegments []models.RecordingSegment
	for _, segment := range candidates {
		hours := retention[segment.StreamName]
		if hours <= 0 {
			hours = m.cfg.RetainHours
		}
		if recordingExpired(segment.EndedAt, now, hours) {
			localSegments = append(localSegments, segment)
		}
	}
	for _, seg := range localSegments {
		if seg.FilePath != "" {
			if err := os.Remove(seg.FilePath); err != nil && !os.IsNotExist(err) {
				log.Printf("[recording] 删除过期录像文件失败 %s: %v", seg.FilePath, err)
			}
		}
	}

	// 清理过期的损坏文件：损坏文件不入库，上面基于 DB 的清理覆盖不到，会永久占盘。
	// 它们仍按所属视频源的小时规则清理。
	m.cleanupCorruptedFiles(now, retention)

	// Each live frame sampler rule owns its image retention period. Rules
	// created before this field existed decode to the 24-hour default.
	type frameRetainRule struct{ StreamName, ConfigJSON string }
	var frameRules []frameRetainRule
	database.DB.Table("video_sources").
		Select("video_sources.stream_name, video_analysis_rules.config_json").
		Joins("JOIN video_analysis_rules ON video_analysis_rules.video_source_id = video_sources.id").
		Where("video_analysis_rules.algorithm_code = ?", analysis.AlgorithmFrameSampler).
		Find(&frameRules)
	frameRetention := make(map[string]int, len(frameRules))
	for _, rule := range frameRules {
		frameRetention[rule.StreamName] = analysis.DecodeLiveFrameConfig(rule.ConfigJSON).RetainHours
	}
	var frameCandidates []models.RecordingFrame
	database.DB.Unscoped().Where("captured_at < ?", now.Add(-time.Hour)).Find(&frameCandidates)
	expiredFrames := make([]models.RecordingFrame, 0)
	for _, frame := range frameCandidates {
		hours := frameRetention[frame.StreamName]
		if hours <= 0 {
			hours = 24
		}
		if frame.CapturedAt.Before(now.Add(-time.Duration(hours) * time.Hour)) {
			expiredFrames = append(expiredFrames, frame)
		}
	}
	for _, f := range expiredFrames {
		if f.FilePath != "" {
			if err := os.Remove(f.FilePath); err != nil && !os.IsNotExist(err) {
				log.Printf("[recording] 删除过期帧文件失败 %s: %v", f.FilePath, err)
			}
		}
	}
	if len(expiredFrames) > 0 {
		ids := make([]uint, 0, len(expiredFrames))
		for _, frame := range expiredFrames {
			ids = append(ids, frame.ID)
		}
		database.DB.Unscoped().Where("id IN ?", ids).Delete(&models.RecordingFrame{})
		log.Printf("[recording] 清理过期帧 %d 条", len(expiredFrames))
	}

	// Realtime event evidence has independent retention. A rule may override
	// the deployment default with retain_hours in its generic config JSON.
	type eventRetainRule struct {
		VideoSourceID uint
		AlgorithmCode string
		ConfigJSON    string
	}
	var eventRules []eventRetainRule
	database.DB.Model(&models.VideoAnalysisRule{}).
		Where("algorithm_code <> ?", analysis.AlgorithmFrameSampler).
		Find(&eventRules)
	eventRetention := make(map[string]int, len(eventRules))
	for _, rule := range eventRules {
		eventRetention[eventRetentionKey(rule.VideoSourceID, rule.AlgorithmCode)] =
			decodeEventRetainHours(rule.ConfigJSON, m.cfg.EventRetainHours)
	}
	var eventCandidates []models.AIEvent
	database.DB.Unscoped().Where("started_at < ?", now.Add(-time.Hour)).Find(&eventCandidates)
	expiredEventIDs := make([]uint, 0)
	evidenceRoot := filepath.Join(m.cfg.OutputDir, "_events")
	for _, event := range eventCandidates {
		hours := eventRetention[eventRetentionKey(event.VideoSourceID, event.AlgorithmCode)]
		if hours <= 0 {
			hours = m.cfg.EventRetainHours
		}
		reference := event.StartedAt
		if event.EndedAt != nil {
			reference = *event.EndedAt
		}
		if !reference.Before(now.Add(-time.Duration(hours) * time.Hour)) {
			continue
		}
		removeEvidenceFile(evidenceRoot, event.SnapshotPath)
		removeEvidenceFile(evidenceRoot, event.ClipPath)
		expiredEventIDs = append(expiredEventIDs, event.ID)
	}
	if len(expiredEventIDs) > 0 {
		database.DB.Unscoped().Where("id IN ?", expiredEventIDs).Delete(&models.AIEvent{})
		log.Printf("[analysis] 清理过期AI事件 %d 条", len(expiredEventIDs))
	}

	// 实时抽帧任务只用于可靠调度，图片结果有独立索引；保留7天任务状态即可。
	jobCutoff := time.Now().AddDate(0, 0, -7)
	jobResult := database.DB.Where("input_type = ? AND created_at < ?", "live_stream", jobCutoff).
		Delete(&models.AIJob{})
	if jobResult.RowsAffected > 0 {
		log.Printf("[analysis] 清理历史实时抽帧任务 %d 条", jobResult.RowsAffected)
	}

	segmentIDs := make([]uint, 0, len(localSegments))
	for _, segment := range localSegments {
		segmentIDs = append(segmentIDs, segment.ID)
	}
	if len(segmentIDs) > 0 {
		result := database.DB.Unscoped().Where("id IN ?", segmentIDs).Delete(&models.RecordingSegment{})
		if result.RowsAffected > 0 {
			log.Printf("[recording] 清理过期片段 %d 条", result.RowsAffected)
		}
	}

	m.cleanupEmptyDirs()
}

func recordingExpired(endedAt, now time.Time, retainHours int) bool {
	return retainHours > 0 && endedAt.Before(now.Add(-time.Duration(retainHours)*time.Hour))
}

// cleanupCorruptedFiles 删除已确认损坏且超过保留期的孤立文件。
// 损坏文件不入库，cleanupExpired 基于 DB 的查询覆盖不到它们，否则会永久占盘。
// 以文件 mtime（≈录制时间）判断是否超期；同时清除已消失文件的内存标记，
// 使 corruptedPaths 不会无限增长。进程重启后由 indexExistingFiles 重新探测填充。
func (m *RecorderManager) cleanupCorruptedFiles(now time.Time, retention map[string]int) {
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
		retainHours := retentionHoursForPath(m.cfg.OutputDir, fp, retention, m.cfg.RetainHours)
		if fi.ModTime().Before(now.Add(-time.Duration(retainHours) * time.Hour)) {
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

func retentionHoursForPath(root, filePath string, retention map[string]int, defaultHours int) int {
	relative, err := filepath.Rel(root, filePath)
	if err == nil {
		parts := strings.Split(filepath.Clean(relative), string(os.PathSeparator))
		if len(parts) >= 2 && parts[0] != ".." {
			if hours := retention[parts[0]]; hours > 0 {
				return hours
			}
		}
	}
	if defaultHours > 0 {
		return defaultHours
	}
	return defaultRetainHours
}

func (m *RecorderManager) cleanupZeroByteSegments() {
	where := "file_size = 0"
	args := []interface{}{}
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
		if streamEntry.Name() == "_frames" || streamEntry.Name() == "_events" {
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

func eventRetentionKey(videoSourceID uint, algorithmCode string) string {
	return strconv.FormatUint(uint64(videoSourceID), 10) + ":" + algorithmCode
}

func decodeEventRetainHours(raw string, fallback int) int {
	config := struct {
		RetainHours int `json:"retain_hours"`
	}{}
	if json.Unmarshal([]byte(raw), &config) != nil || config.RetainHours < 1 || config.RetainHours > 87600 {
		return fallback
	}
	return config.RetainHours
}

func removeEvidenceFile(root, target string) {
	if strings.TrimSpace(target) == "" {
		return
	}
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return
	}
	targetAbs, err := filepath.Abs(target)
	if err != nil {
		return
	}
	rel, err := filepath.Rel(rootAbs, targetAbs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		log.Printf("[analysis] 拒绝删除_events目录外的证据文件 %s", target)
		return
	}
	if err := os.Remove(targetAbs); err != nil && !os.IsNotExist(err) {
		log.Printf("[analysis] 删除过期AI证据失败 %s: %v", targetAbs, err)
	}
}

func lookupVideoSource(streamName string) (customerID uint, mac, sourceType, sourceID string) {
	var source models.VideoSource
	if err := database.DB.Where("stream_name = ?", streamName).First(&source).Error; err == nil {
		return source.CustomerID, source.MAC, source.SourceType, source.SourceID
	}
	mac, sourceType, ok := streamsource.Parse(streamName)
	if !ok {
		return 0, streamName, "unknown", ""
	}
	if sourceType == streamsource.TypeScreen {
		sourceID = "desktop"
	}
	return 0, mac, sourceType, sourceID
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
