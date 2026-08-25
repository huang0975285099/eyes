package main

import (
	"context"
	"log"
	"media-service/analysis"
	"media-service/config"
	"media-service/database"
	"media-service/recording"
	"media-service/web"
	"os"
	"os/signal"
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

	mgr := recording.NewRecorderManager(recording.Config{
		SRSApiBase:      cfg.Recording.SRSApiBase,
		RTMPHost:        cfg.Recording.RTMPHost,
		OutputDir:       cfg.Recording.OutputDir,
		SegmentDuration: cfg.Recording.SegmentDuration,
		CheckInterval:   cfg.Recording.CheckInterval,
		RetainHours:     cfg.Recording.RetainHours,
		FrameRetainDays: cfg.Recording.FrameRetainDays,
		FFmpegPath:      cfg.Recording.FFmpegPath,
	})

	ctx, cancel := context.WithCancel(context.Background())

	go mgr.Run(ctx)
	log.Println("[main] MediaService 已启动")

	if cfg.Recording.WebPort > 0 {
		go web.NewServer(
			cfg.Recording.SRSApiBase,
			cfg.Recording.SRSHttpHost,
			cfg.Recording.PublicRTMPHost,
			cfg.Recording.RTMPHost,
			cfg.Recording.AIStreamBaseURL,
			cfg.Recording.OutputDir,
			cfg.Recording.RetainHours,
			mgr.RefreshRules,
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
