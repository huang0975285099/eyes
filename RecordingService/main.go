package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"recording-service/analysis"
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

	if err := database.Init(cfg); err != nil {
		log.Fatalf("[main] 数据库初始化失败: %v", err)
	}
	defer func() {
		if err := database.Close(); err != nil {
			log.Printf("[main] 关闭数据库连接失败: %v", err)
		}
	}()
	if err := analysis.InitializeCatalog(); err != nil {
		log.Fatalf("[main] 初始化AI分析任务失败: %v", err)
	}

	// 启动后尝试从 DB 加载保留天数，覆盖环境变量
	var recordingSetting models.RecordingSetting
	recordEnabled := true
	if err := database.DB.First(&recordingSetting).Error; err == nil {
		cfg.Recording.RetainDays = recordingSetting.RetainDays
		recordEnabled = recordingSetting.RecordEnabled
		log.Printf("[main] 从 DB 加载保留天数: %d 天（覆盖环境变量）", recordingSetting.RetainDays)
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
		RecordEnabled:   recordEnabled,
	})

	ctx, cancel := context.WithCancel(context.Background())

	go mgr.Run(ctx)
	log.Println("[main] RecordingService 已启动")

	if cfg.Recording.WebPort > 0 {
		go web.NewServer(
			cfg.Recording.SRSApiBase,
			cfg.Recording.SRSHttpHost,
			cfg.Recording.PublicRTMPHost,
			cfg.Recording.OutputDir,
			cfg.Recording.RetainDays,
			recordEnabled,
			mgr.UpdateRetainDays, // 热更新回调
			mgr.UpdateRecordEnabled,
			cfg.Security.ClientAPIKey,
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
