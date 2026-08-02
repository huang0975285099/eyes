package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"recording-service/config"
	"recording-service/database"
	"recording-service/models"
	"recording-service/recording"
	"recording-service/web"
	"syscall"

	"github.com/joho/godotenv"
)

func main() {
	if err := godotenv.Load(); err != nil {
		log.Println("[main] 未找到 .env 文件，使用环境变量")
	}

	cfg := config.Load()

	// 确定节点标识：优先环境变量，否则使用 hostname
	nodeID := cfg.Recording.NodeID
	if nodeID == "" {
		if hostname, err := os.Hostname(); err == nil {
			nodeID = hostname
		} else {
			nodeID = "default-node"
		}
	}
	cfg.Recording.NodeID = nodeID
	log.Printf("[main] 本节点标识 NodeID=%s, NodeName=%s", nodeID, cfg.Recording.NodeName)

	// 初始 ZoneIDs 来自环境变量（envZoneIDs）
	envZoneIDs := cfg.Recording.NodeZoneIDs
	if len(envZoneIDs) == 0 || (len(envZoneIDs) == 1 && envZoneIDs[0] == 0) {
		log.Println("[main] 环境变量 RECORDING_NODE_ZONE_ID=0，默认录制所有车间")
	} else {
		log.Printf("[main] 环境变量 ZoneIDs=%v", envZoneIDs)
	}

	database.Init(cfg)

	// 启动后尝试从 DB 加载车间分配，覆盖环境变量
	var initZoneIDs []uint
	dbZoneIDs := loadZoneIDsFromDB(nodeID)
	if dbZoneIDs != nil {
		initZoneIDs = dbZoneIDs
		log.Printf("[main] 从 DB 加载车间分配 ZoneIDs=%v（覆盖环境变量）", initZoneIDs)
	} else {
		initZoneIDs = envZoneIDs
		log.Printf("[main] DB 中无本节点车间分配，使用环境变量 ZoneIDs=%v", initZoneIDs)
	}

	if cfg.Recording.CenterURL != "" {
		log.Printf("[main] 抽帧推送模式：帧将上传至 %s", cfg.Recording.CenterURL)
	} else {
		log.Println("[main] 抽帧本地模式：帧保存在本节点磁盘")
	}

	// 启动后尝试从 DB 加载保留天数，覆盖环境变量
	var nodeSetting models.NodeSetting
	if err := database.DB.Where("node_id = ?", nodeID).First(&nodeSetting).Error; err == nil {
		cfg.Recording.RetainDays = nodeSetting.RetainDays
		log.Printf("[main] 从 DB 加载保留天数: %d 天（覆盖环境变量）", nodeSetting.RetainDays)
	} else {
		log.Printf("[main] DB 中无本节点保留天数配置，使用环境变量: %d 天", cfg.Recording.RetainDays)
	}

	mgr := recording.NewRecorderManager(recording.Config{
		SRSApiBase:      cfg.Recording.SRSApiBase,
		RTMPHost:        cfg.Recording.RTMPHost,
		OutputDir:       cfg.Recording.OutputDir,
		SegmentDuration: cfg.Recording.SegmentDuration,
		CheckInterval:   cfg.Recording.CheckInterval,
		RetainDays:      cfg.Recording.RetainDays,
		FrameRetainDays: cfg.Recording.FrameRetainDays,
		FFmpegPath:      cfg.Recording.FFmpegPath,
		NodeZoneIDs:     initZoneIDs,
		CenterURL:       cfg.Recording.CenterURL,
		CenterKey:       cfg.Recording.CenterKey,
	})

	// 共享同一个 ZoneConfig，后台修改后两者同时生效
	zoneCfg := mgr.ZoneConfig()

	ctx, cancel := context.WithCancel(context.Background())

	go mgr.Run(ctx)
	log.Println("[main] RecordingService 已启动")

	if cfg.Recording.WebPort > 0 {
		go web.NewServer(
			zoneCfg,
			nodeID,
			cfg.Recording.NodeName,
			envZoneIDs, // 环境变量初始值，DB 无配置时回退
			cfg.Recording.SRSApiBase,
			cfg.Recording.SRSHttpHost,
			cfg.Recording.PublicRTMPHost,
			cfg.Recording.RetainDays,
			mgr.UpdateRetainDays, // 热更新回调
			cfg.Security.ClientAPIKey,
			cfg.Security.StreamTokenSecret,
		).Start(cfg.Recording.WebPort)
	}

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	sig := <-quit
	log.Printf("[main] 收到信号 %v，正在优雅关闭...", sig)

	cancel()
	mgr.Wait()
	log.Println("[main] 优雅关闭完成")
}

// loadZoneIDsFromDB 从 zone_assignments 表加载指定节点的 Zone ID 列表。
// 返回 nil 表示 DB 中无该节点的配置，调用方应回退到环境变量。
func loadZoneIDsFromDB(nodeID string) []uint {
	if nodeID == "" || database.DB == nil {
		return nil
	}
	var assignments []struct {
		ZoneID uint
	}
	if err := database.DB.Table("zone_assignments").
		Where("node_id = ?", nodeID).
		Order("zone_id ASC").
		Find(&assignments).Error; err != nil {
		log.Printf("[main] 查询 zone_assignments 失败: %v", err)
		return nil
	}
	if len(assignments) == 0 {
		return nil
	}
	ids := make([]uint, 0, len(assignments))
	for _, a := range assignments {
		ids = append(ids, a.ZoneID)
	}
	return ids
}
